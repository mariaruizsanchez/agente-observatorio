"""
El agente Text-to-SQL.

Flujo de una pregunta:
  1. Se le pasa al modelo la pregunta + el esquema de la BBDD + el historial
     de la conversación.
  2. El modelo devuelve una consulta SQL (o pide una aclaración).
  3. La consulta se ejecuta contra Azure SQL.
  4. El modelo redacta una respuesta en lenguaje natural con el resultado.

El agente recuerda los mensajes anteriores: así, si el usuario responde
"Bizkaia" a una pregunta de aclaración, el modelo entiende que completa la
pregunta anterior.

El cliente de IA usa el SDK de OpenAI, que es compatible con GitHub Models,
OpenAI y Azure OpenAI. El proveedor concreto se decide en config.py / .env,
así que este archivo NO cambia al cambiar de proveedor.
"""

from openai import OpenAI
import config
import database


# Cliente de IA: la URL base y la clave vienen de la configuración.
# Cambiar de GitHub Models a OpenAI/Azure se hace editando el .env, no aquí.
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
                 "el resultado es parcial y que conviene concretar la pregunta.")

    respuesta = cliente.chat.completions.create(
        model=config.AI_MODEL,
        messages=[
            {"role": "system", "content": (
                "Redactas respuestas claras y breves en español a partir de "
                "resultados de consultas SQL. No inventes datos. Presenta "
                "todos los datos del resultado, no solo una parte."
            )},
            {"role": "user", "content": (
                f"Pregunta del usuario: {pregunta}\n\n"
                f"Consulta ejecutada: {sql}\n\n"
                f"Resultado ({len(filas)} filas):\n{tabla_texto}{aviso}\n\n"
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
