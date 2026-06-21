"""
El agente Text-to-SQL.

Flujo de una pregunta:
  1. Se le pasa al modelo la pregunta + el esquema de la BBDD + el historial
     de la conversacion.
  2. El modelo devuelve una consulta SQL (o pide una aclaracion).
  3. La consulta se ejecuta contra Azure SQL.
  4. El modelo redacta una respuesta en lenguaje natural con el resultado.

El agente recuerda los mensajes anteriores: asi, si el usuario responde
"Bizkaia" a una pregunta de aclaracion, el modelo entiende que completa la
pregunta anterior.

El cliente de IA usa el SDK de OpenAI, que es compatible con GitHub Models,
OpenAI y Azure OpenAI. El proveedor concreto se decide en config.py / .env,
asi que este archivo NO cambia al cambiar de proveedor.
"""

from openai import OpenAI
import config
import database


# Cliente de IA: la URL base y la clave vienen de la configuracion.
# Cambiar de GitHub Models a OpenAI/Azure se hace editando el .env, no aqui.
cliente = OpenAI(base_url=config.AI_BASE_URL, api_key=config.AI_API_KEY)


def _prompt_sistema(esquema, diccionario, territorios):
    """Construye las instrucciones del sistema, con el esquema y el diccionario."""
    bloque_diccionario = ""
    if diccionario:
        bloque_diccionario = f"""
Diccionario de datos (descripcion y VALORES REALES de cada columna):
{diccionario}

IMPORTANTE: cuando filtres por una columna de texto en una clausula WHERE,
usa EXACTAMENTE uno de los valores posibles indicados en el diccionario.
No inventes ni abrevies valores.
"""

    bloque_territorios = ""
    if territorios:
        bloque_territorios = f"""
Territorios disponibles en cada tabla:
{territorios}
"""

    return f"""Eres un asistente experto en SQL para el Observatorio Sociolaboral
con perspectiva de genero en Euskadi. Traduces preguntas en lenguaje natural
a consultas SQL para Microsoft SQL Server (Azure SQL Database).

Esquema de la base de datos:
{esquema}
{bloque_diccionario}{bloque_territorios}
Reglas:
- Genera SOLO consultas SELECT de lectura. Nunca modifiques datos.
- Usa la sintaxis de SQL Server (por ejemplo, TOP en lugar de LIMIT).
- Usa unicamente tablas y columnas que existan en el esquema de arriba.
- Si la pregunta no se puede responder con los datos disponibles, dilo.

REGLA DEL TERRITORIO (obligatoria, compruebala SIEMPRE antes de responder):
Mira la lista "Territorios disponibles en cada tabla" de arriba y localiza la
tabla que vas a consultar. Cada fila de datos pertenece a un territorio
concreto y los territorios NO se pueden mezclar en una misma consulta.
- Si esa tabla tiene UN SOLO territorio en la lista: usa ese directamente con
  WHERE Territorio = '...'. NO preguntes nada al usuario, solo hay una opcion.
- Si esa tabla tiene VARIOS territorios y la conversacion YA indica cual
  quiere el usuario: genera la consulta con su filtro WHERE Territorio = '...'.
- Si esa tabla tiene VARIOS territorios y la conversacion NO indica cual:
  NO generes SQL. Responde unicamente con 'ACLARAR:' seguido de una pregunta
  ofreciendo EXACTAMENTE los territorios que esa tabla tiene en la lista.
Nunca ofrezcas un territorio que no aparezca en la lista para esa tabla.

REGLA DEL SEXO (perspectiva de genero del Observatorio):
Muchas tablas tienen la columna Sexo (valores: Hombre, Mujer). Siempre que la
tabla consultada tenga columna Sexo, INCLUYE esa columna en el SELECT y en el
GROUP BY / ORDER BY, para que los resultados queden desglosados por sexo.
No mezcles ni promedies hombres y mujeres en un mismo valor. Este desglose es
el objetivo central del Observatorio, asi que aplicalo aunque la pregunta no
mencione el sexo explicitamente.

REGLA DEL AÑO (comportamiento por defecto, similar a un filtro de año):
Casi todas las tablas tienen la columna Año.
- Si la pregunta menciona un año concreto (por ejemplo "en 2022"), filtra por
  ese año.
- Si la pregunta pide explicita o implicitamente varios años (con palabras
  como "evolucion", "por año", "historico", "tendencia", "cada año", "desde
  ... hasta ..."), devuelve todos los años, ordenados por año.
- Si la pregunta NO menciona ningun año ni indica varios años, devuelve
  UNICAMENTE el dato del ultimo año disponible. Para ello filtra con
  WHERE Año = (SELECT MAX(Año) FROM <la misma tabla>), e INCLUYE SIEMPRE la
  columna Año in el SELECT (para que se sepa de que año son los datos). Este
  es el comportamiento por defecto, equivalente a un filtro de año en el
  ultimo periodo disponible.
"""


def _limpiar_sql(texto):
    """Quita los bloques de codigo ```sql ... ``` si el modelo los añade."""
    sql = texto.strip()
    if sql.startswith("```"):
        sql = sql.split("```")[1]
        if sql.lower().startswith("sql"):
            sql = sql[3:]
    return sql.strip()


def _pedir_sql(historial):
    """
    Primera llamada al modelo: convierte la conversacion en una consulta SQL.
    'historial' es la lista de mensajes (incluido el de sistema).
    """
    respuesta = cliente.chat.completions.create(
        model=config.AI_MODEL,
        messages=historial,
        temperature=0,
    )
    texto = respuesta.choices[0].message.content.strip()
    if texto.upper().startswith("ACLARAR"):
        return texto
    return _limpiar_sql(texto)


def _redactar_respuesta(pregunta, sql, cabeceras, filas):
    """Segunda llamada al modelo: redacta la respuesta en lenguaje natural."""
    # Enviamos hasta 500 filas al modelo. Es suficiente para tablas de
    # evolucion por año/territorio sin disparar el consumo de tokens.
    LIMITE = 500
    muestra = filas[:LIMITE]
    tabla_texto = " | ".join(cabeceras) + "\n"
    tabla_texto += "\n".join(" | ".join(str(c) for c in fila) for fila in muestra)

    aviso = ""
    if len(filas) > LIMITE:
        aviso = (f"\n\nNOTA: la consulta devolvio {len(filas)} filas pero solo "
                 f"se muestran las primeras {LIMITE}. Avisa al usuario de que "
                 f"el resultado es parcial y que conviene concretar la pregunta.")

    # =========================================================================
    # NUEVA LÓGICA DE CÁLCULO SEGURO EN PYTHON (EVITA ALUCINACIONES MATEMÁTICAS)
    # =========================================================================
    bloque_calculos_python = ""
    try:
        # Normalizamos cabeceras a minúsculas para un emparejamiento seguro
        cabeceras_min = [c.lower() for c in cabeceras]
        
        if "sexo" in cabeceras_min:
            idx_sexo = cabeceras_min.index("sexo")
            
            # Buscamos la columna numérica dinámica (aquella que no sea metadato)
            idx_valor = None
            for i, col in enumerate(cabeceras_min):
                if col not in ["año", "sexo", "territorio", "id"]:
                    idx_valor = i
                    break
            
            if idx_valor is not None:
                # Agrupamos los datos por dimensiones (Año, Territorio, etc.) para calcular la brecha de cada grupo por separado
                grupos = {}
                for fila in filas:
                    # Creamos una tupla identificadora excluyendo las columnas dinámicas (Sexo y el Valor numérico)
                    clave = tuple(str(fila[i]) for i, col in enumerate(cabeceras_min) if i != idx_sexo and i != idx_valor)
                    if clave not in grupos:
                        grupos[clave] = {}
                    
                    sexo_valor = str(fila[idx_sexo]).strip().lower()
                    try:
                        grupos[clave][sexo_valor] = float(fila[idx_valor])
                    except (ValueError, TypeError):
                        pass

                # Ejecutamos las operaciones matemáticas aritméticas reales en la CPU
                lineas_calculadas = []
                for clave, sexos in grupos.items():
                    if "hombre" in sexos and "mujer" in sexos:
                        val_h = sexos["hombre"]
                        val_m = sexos["mujer"]
                        
                        if val_h > 0:
                            dif_absoluta = round(val_h - val_m, 2)
                            brecha_porcentual = round(((val_h - val_m) / val_h) * 100, 1)
                            
                            # Si hay agrupaciones por más variables (ej: múltiples años), lo indicamos contextualmente
                            info_grupo = f"Para el grupo {list(clave)}: " if clave and any(clave) else ""
                            lineas_calculadas.append(
                                f"- {info_grupo}Diferencia absoluta exacta: {dif_absoluta}. "
                                f"Brecha porcentual exacta: {brecha_porcentual}%."
                            )
                
                if lineas_calculadas:
                    bloque_calculos_python = "\n".join(lineas_calculadas)
    except Exception:
        # Mecanismo de seguridad pasiva: si hay un error imprevisto analizando los datos,
        # la aplicación no se bloquea y deja que el flujo original continúe.
        pass

    # Adjuntamos las métricas exactas calculadas por Python para que el LLM solo las transcriba
    instruccion_calculos = ""
    if bloque_calculos_python:
        instruccion_calculos = (
            f"\nDATOS MATEMÁTICOS CALCULADOS POR EL SISTEMA (USA ESTOS, NO CALCULES TÚ NADA):\n"
            f"{bloque_calculos_python}\n"
        )
    # =========================================================================

    respuesta = cliente.chat.completions.create(
        model=config.AI_MODEL,
        messages=[
            {"role": "system", "content": (
                "Redactas respuestas claras y breves en español a partir de "
                "resultados de consultas SQL. No inventes datos. Presenta "
                "todos los datos del resultado, no solo una parte.\n\n"
                "PERSPECTIVA DE GÉNERO (obligatorio siempre que el resultado "
                "incluya datos de Hombre y de Mujer):\n"
                "1. Presenta la respuesta SEPARADA en dos bloques claramente "
                "diferenciados: primero los datos de Hombres y despues los "
                "datos de Mujeres. No los mezcles en una sola lista.\n"
                "2. Despues de los dos bloques, añade SIEMPRE un apartado con "
                "la brecha de genero.\n"
                "REGLA DE ORO MÁXIMA: Usa exclusivamente los datos numéricos de brechas "
                "y diferencias absolutas proporcionados en la sección 'DATOS MATEMÁTICOS CALCULADOS POR EL SISTEMA'. "
                "No intentes restar ni dividir tú los valores de las filas por tu cuenta, limítate a copiar las cifras del sistema.\n"
                "Debes enunciar la brecha porcentual EXACTAMENTE con esta estructura: "
                "«la brecha es del X %; las mujeres [verbo adecuado al contexto, ej. perciben/tienen/registran] un X % menos que los hombres». "
                "REGLA ESTRICTA: Bajo ninguna circunstancia utilices la base femenina para el calculo ni uses "
                "la formula o estructura 'los hombres ganan/tienen un Y % mas'. "
                "Ademas, adapta los detalles adicionales segun el tipo de dato: para importes (salarios, rentas) "
                "añade tambien la diferencia absoluta exacta que te proporciona el sistema; para tasas y porcentajes expresa la diferencia en puntos porcentuales; "
                "para recuentos usa la diferencia absoluta.\n"
                "3. Si hay varios años o categorias, aplica este desglose y "
                "esta diferencia para cada uno de ellos.\n\n"
                "AVISO DEL AÑO: si la consulta SQL filtra por un unico año "
                "mediante MAX(Año) (es decir, se ha devuelto el ultimo año "
                "disponible por defecto), indica al final esta frase: 'Los "
                "datos son del año mas reciente disponible (AAAA).', "
                "sustituyendo AAAA por el año concreto de los datos del "
                "resultado. Añade ademas que se puede pedir la evolucion "
                "completa de todos los años."
            )},
            {"role": "user", "content": (
                f"Pregunta del usuario: {pregunta}\n\n"
                f"Consulta ejecutada: {sql}\n\n"
                f"Resultado ({len(filas)} filas):\n{tabla_texto}{aviso}\n\n"
                f"{instruccion_calculos}\n"
                "Redacta la respuesta para el usuario."
            )},
        ],
        temperature=0.2,
    )
    return respuesta.choices[0].message.content.strip()


def responder(pregunta, esquema, diccionario="", territorios="",
              mapa_territorios=None, historial=None, _reintentos=0):
    """
    Procesa una pregunta de principio a fin.

    'historial' es la lista de turnos previos de la conversacion, en formato
    [{"role": "user"/"assistant", "content": ...}, ...]. Permite que el agente
    recuerde mensajes anteriores (por ejemplo, una aclaracion de territorio).

    'mapa_territorios' es {tabla: [territorios]}. Sirve para resolver solo,
    sin molestar al usuario, las aclaraciones de tablas que tienen un unico
    territorio posible.

    Devuelve un diccionario con:
      - 'sql': la consulta generada (o None si pidio aclaracion)
      - 'respuesta': el texto para el usuario
      - 'historial': el historial actualizado, para pasarlo a la siguiente
        llamada.
    """
    historial = list(historial or [])
    mapa_territorios = mapa_territorios or {}

    # Construimos los mensajes: sistema + turnos previos + pregunta actual.
    mensajes = [{"role": "system",
                 "content": _prompt_sistema(esquema, diccionario, territorios)}]
    mensajes += historial
    mensajes.append({"role": "user", "content": pregunta})

    sql = _pedir_sql(mensajes)

    # Caso 1: el modelo pide una aclaracion.
    if sql.upper().startswith("ACLARAR"):
        aclaracion = sql[len("ACLARAR:"):].strip() if ":" in sql else sql

        # Si la aclaracion solo menciona UN territorio de los que existen,
        # significa que la tabla tiene una unica opcion: la respondemos solas
        # sin molestar al usuario. (Evita el "elige: C.A. de Euskadi".)
        territorios_unicos = {t[0] for t in mapa_territorios.values()
                              if len(t) == 1}
        mencionados = [t for t in territorios_unicos if t in aclaracion]
        if len(mencionados) == 1 and _reintentos < 2:
            # Reintentamos la consulta dando por respondido el territorio.
            hist = historial + [
                {"role": "user", "content": pregunta},
                {"role": "assistant", "content": sql},
            ]
            return responder(mencionados[0], esquema, diccionario, territorios,
                             mapa_territorios, hist, _reintentos + 1)

        # Aclaracion legitima (varias opciones): se la pasamos al usuario.
        nuevo_historial = historial + [
            {"role": "user", "content": pregunta},
            {"role": "assistant", "content": sql},
        ]
        return {"sql": None, "respuesta": aclaracion,
                "cabeceras": None, "filas": None,
                "historial": nuevo_historial}

    # Caso 2: el modelo genero SQL. Lo ejecutamos.
    try:
        cabeceras, filas = database.ejecutar_consulta(sql)
    except Exception as e:
        return {"sql": sql, "respuesta": f"No se pudo ejecutar la consulta: {e}",
                "cabeceras": None, "filas": None, "historial": historial}

    texto = _redactar_respuesta(pregunta, sql, cabeceras, filas)
    # Tras una respuesta completa, reiniciamos el historial: la siguiente
    # pregunta empieza de cero (evita arrastrar contexto indefinidamente).
    # Las filas se convierten a listas normales para que sean fáciles de usar
    # desde la interfaz web.
    return {"sql": sql, "respuesta": texto,
            "cabeceras": cabeceras,
            "filas": [list(f) for f in filas],
            "historial": []}
