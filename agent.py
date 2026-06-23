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

import re

from openai import OpenAI
import config
import database


# Cliente de IA: la URL base y la clave vienen de la configuracion.
# Cambiar de GitHub Models a OpenAI/Azure se hace editando el .env, no aqui.
cliente = OpenAI(base_url=config.AI_BASE_URL, api_key=config.AI_API_KEY)


# =============================================================================
# CONFIGURACION DE LA BRECHA (ajustable sin tocar la logica)
# =============================================================================
# FIX (bug 5): tablas donde la columna Sexo describe AL SUJETO y NO representa
# una brecha de genero comparable (p. ej. el sexo de los recien nacidos). En
# esas tablas no se calcula brecha. La comparacion es por SUBCADENA, asi que
# "Demografia_Nacimientos" coincide tanto si la tabla en SQL se llama
# Demografia_Nacimientos como tbl_Demografia_Nacimientos.
TABLAS_SIN_BRECHA = ("Demografia_Nacimientos", "Demografia_Jovenes")

# FIX (bug 2): pistas para clasificar el tipo de indicador por el nombre de la
# columna de valor, y decidir como se expresa la brecha (pp / % / recuento).
_PISTAS_TASA = ("tasa", "porcentaje", "ratio", "%")
_PISTAS_IMPORTE = ("salario", "renta", "importe", "ingreso", "euro", "€", "ganancia")

# Palabras que indican que el usuario pide la evolucion (varios años).
_PALABRAS_EVOLUCION = ("evolu", "por año", "historic", "histórico", "tendencia",
                       "cada año", "anual", "desde", "hasta", "serie")

# FIX (bug nuevo, comparacion territorial): marcadores de que el usuario quiere
# COMPARAR territorios o saber en cual es mayor/menor un indicador. Cuando
# aparecen, NO se pide aclaracion de territorio: se agrupa por Territorio y se
# devuelven todos.
_PALABRAS_COMPARA_TERRITORIO = (
    "en qué territorio", "en que territorio", "qué territorio", "que territorio",
    "cuál territorio", "cual territorio", "qué provincia", "que provincia",
    "cuál provincia", "cual provincia", "entre territorios", "entre provincias",
    "compara", "comparar", "comparativa", "ranking", "por territorio",
    "por provincia", "cada territorio", "cada provincia",
)


def _texto_conversacion(historial, pregunta):
    """
    FIX (bug 1, comparacion): une el texto de TODOS los turnos de usuario del
    hilo + la pregunta actual, en minusculas. Asi la deteccion de intencion
    (evolucion, comparacion, año explicito) sigue funcionando aunque la señal
    este en un turno anterior. Caso tipico: "Evolucion de la tasa de paro por
    año en Bizkaia" -> el agente pide "Hombre/Mujer/ambos" -> el usuario
    responde "Mujer". La palabra "evolucion" esta en el primer turno, no en
    "Mujer"; sin esto, la red de "año por defecto" colapsaba la serie a un año.
    """
    partes = [m.get("content", "") for m in (historial or [])
              if m.get("role") == "user"]
    partes.append(pregunta or "")
    return " ".join(partes).lower()


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
- Si la tabla NO aparece en esa lista (o figura como "tabla SIN dimension
  territorial"): el indicador es un agregado que no se desglosa por territorio.
  En ese caso NO pidas territorio, NO añadas WHERE Territorio y NO ofrezcas
  ninguna aclaracion territorial. Responde con el dato agregado tal cual. Esto
  aplica, por ejemplo, a los salarios, que son un agregado de Euskadi.
- Si esa tabla tiene UN SOLO territorio en la lista: usa ese directamente con
  WHERE Territorio = '...'. NO preguntes nada al usuario, solo hay una opcion.
- Si esa tabla tiene VARIOS territorios y la conversacion YA indica cual
  quiere el usuario: genera la consulta con su filtro WHERE Territorio = '...'.
- Si esa tabla tiene VARIOS territorios y la conversacion NO indica cual:
  NO generes SQL. Responde unicamente con 'ACLARAR:' seguido de una pregunta
  ofreciendo EXACTAMENTE los territorios que esa tabla tiene en la lista.
Nunca ofrezcas un territorio que no aparezca en la lista para esa tabla.

REGLA DE COMPARACION ENTRE TERRITORIOS (tiene prioridad sobre la aclaracion):
Si la pregunta compara territorios o pregunta en cual es mayor/menor un
indicador (por ejemplo "en que territorio es mayor...", "compara X entre
Araba, Bizkaia y Gipuzkoa", "ranking por territorio"):
- NO pidas aclaracion de territorio y NO filtres por un unico territorio.
- Incluye Territorio en el SELECT y en el GROUP BY y devuelve TODOS los
  territorios de esa tabla, ordenados por el valor del indicador.
- Si esa tabla solo tiene un territorio (o no tiene dimension territorial),
  NO inventes una comparacion: responde que ese indicador no se desglosa por
  territorio.

REGLA DE SECTOR ECONOMICO:
Si la pregunta menciona un sector o rama de actividad concreto (industria,
construccion, servicios, agricultura, etc.), localiza la tabla y la COLUMNA de
sector/rama y filtra por ese sector (WHERE <columna_sector> = '...'). Usa
SIEMPRE la misma tabla de ocupacion por sector con independencia de como se
formule la pregunta: "personas ocupadas en la industria", "mujeres en el
sector industria" o "ocupacion industrial" deben dar el MISMO dato. Nunca
devuelvas el total de ocupacion de todos los sectores cuando se pide un sector
concreto.

REGLA DEL SEXO (perspectiva de genero del Observatorio):
Muchas tablas tienen la columna Sexo (valores: Hombre, Mujer). Siempre que la
tabla consultada tenga columna Sexo, INCLUYE esa columna en el SELECT y en el
GROUP BY / ORDER BY, para que los resultados queden desglosados por sexo.
No mezcles ni promedies hombres y mujeres en un mismo valor. Este desglose es
el objetivo central del Observatorio, asi que aplicalo aunque la pregunta no
mencione el sexo explicitamente.
No calcules tu mismo en la SQL porcentajes ni ratios entre sexos (no crees
columnas del tipo "Porcentaje_Mujeres"): el sistema calcula las proporciones y
las brechas a partir de los recuentos. Limitate a devolver los valores por
sexo.

REGLA DEL AÑO (comportamiento por defecto, similar a un filtro de año):
Casi todas las tablas tienen la columna Año.
- Si la pregunta menciona un año concreto (por ejemplo "en 2022"), filtra por
  ese año.
- Si la pregunta pide explicita o implicitamente varios años (con palabras
  como "evolucion", "por año", "historico", "tendencia", "cada año", "desde
  ... hasta ..."), devuelve todos los años, ordenados por año, SIN filtrar por
  un unico año.
- Si la pregunta NO menciona ningun año ni indica varios años, devuelve
  UNICAMENTE el dato del ultimo año disponible. Para ello filtra con
  WHERE Año = (SELECT MAX(Año) FROM <la misma tabla>), e INCLUYE SIEMPRE la
  columna Año en el SELECT (para que se sepa de que año son los datos). Este
  es el comportamiento por defecto, equivalente a un filtro de año en el
  ultimo periodo disponible.
- ESTA REGLA SE APLICA TAMBIEN A LAS PREGUNTAS DE BRECHA, DIFERENCIA O
  COMPARACION ENTRE SEXOS. "Cual es la brecha salarial?" NO menciona año, asi
  que debe devolver SOLO el ultimo año disponible (con WHERE Año = (SELECT
  MAX(Año) ...)), NUNCA un promedio de varios años. Incluye SIEMPRE la columna
  Año en el SELECT y en el GROUP BY, para que la brecha se calcule sobre un
  año concreto y no sobre una media multianual.

REGLA DE AGREGACION:
Si una tabla tiene VARIAS filas para una misma combinacion de Año, Territorio y
Sexo (por ejemplo varias cohortes de edad, tramos o categorias) y la pregunta
pide un total o un dato global, AGREGA con SUM(...) y agrupa con
GROUP BY Año, Territorio, Sexo. No devuelvas las filas de detalle una a una sin
agregar cuando lo que se pide es un total.
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


# =============================================================================
# CALCULO DE LA BRECHA (deterministico, en Python)
# =============================================================================
def _tabla_de_sql(sql):
    """Extrae el nombre de la tabla principal de la consulta (clausula FROM)."""
    m = re.search(r"\bFROM\s+\[?([A-Za-z0-9_]+)\]?", sql, re.IGNORECASE)
    return m.group(1) if m else ""


def _tipo_indicador(nombre_columna, valores):
    """
    FIX (bug 2): clasifica el indicador para expresar bien la brecha.
      - 'tasa'     -> diferencia en puntos porcentuales (pp)
      - 'importe'  -> % relativo (base hombres) + diferencia absoluta en €
      - 'recuento' -> diferencia absoluta
    Primero por el nombre de la columna; como respaldo, por la magnitud (las
    tasas se almacenan como fracciones, 0-1).
    """
    n = (nombre_columna or "").lower()
    if any(p in n for p in _PISTAS_TASA):
        return "tasa"
    if any(p in n for p in _PISTAS_IMPORTE):
        return "importe"
    try:
        nums = [abs(float(v)) for v in valores if v is not None]
        if nums and all(x <= 1.5 for x in nums):
            return "tasa"
    except (ValueError, TypeError):
        pass
    return "recuento"


def _frase_brecha(val_h, val_m, tipo):
    """
    FIX (bug 2, 4): devuelve la frase de brecha ya redactada, con la unidad
    correcta segun el tipo y con el SENTIDO (mas/menos) calculado segun quien
    sea mayor. Asi se evita el texto roto del tipo "las mujeres registran X
    menos" cuando en realidad tienen mas.
    """
    if tipo == "tasa":
        # Las tasas pueden venir como fraccion (0,162) o como % (16,2).
        escala = 100 if max(abs(val_h), abs(val_m)) <= 1.5 else 1
        pp = round(abs(val_h - val_m) * escala, 1)
        sentido = "menor" if val_m <= val_h else "mayor"
        return (f"la brecha es de {pp} puntos porcentuales; la tasa de las "
                f"mujeres es {pp} pp {sentido} que la de los hombres.")

    if tipo == "importe":
        dif = round(abs(val_h - val_m), 2)
        base = val_h if val_h else 1
        pct = round(abs(val_h - val_m) / base * 100, 1)
        sentido = "menos" if val_m <= val_h else "mas"
        return (f"la brecha es del {pct} %; las mujeres perciben un {pct} % "
                f"{sentido} que los hombres (diferencia absoluta de {dif} €).")

    # recuento
    # FIX (bug 4): ademas de la diferencia absoluta, añadimos la PROPORCION de
    # mujeres sobre el total (mujeres / (hombres + mujeres)). Es la cifra
    # correcta para preguntas del tipo "que porcentaje de electas son mujeres"
    # (~59 %), que el modelo calculaba mal como 0 %/100 % por sexo.
    total = abs(val_h) + abs(val_m)
    dif = round(abs(val_h - val_m))
    sentido = "menos" if val_m <= val_h else "mas"
    linea = (f"la diferencia es de {dif}; las mujeres registran {dif} {sentido} "
             f"que los hombres")
    if total > 0:
        pct_m = round(abs(val_m) / total * 100, 1)
        linea += f" (las mujeres representan el {pct_m} % del total)"
    return linea + "."


def _calcular_brechas(sql, cabeceras, cabeceras_min, filas):
    """
    Construye el bloque de brechas (una linea por grupo: año, territorio...).
    Devuelve "" si no procede (tabla no comparable, sin sexo, o sin columna
    numerica).
    """
    # FIX (bug 5): no calcular brecha en indicadores no comparables.
    tabla = _tabla_de_sql(sql)
    if any(t.lower() in tabla.lower() for t in TABLAS_SIN_BRECHA):
        return ""
    if "sexo" not in cabeceras_min:
        return ""

    idx_sexo = cabeceras_min.index("sexo")

    # FIX (bug 4): columnas de porcentaje/ratio precalculadas que el modelo
    # pudiera haber colado (p. ej. "Porcentaje_Mujeres" con 0/100 por sexo). Se
    # ignoran POR COMPLETO: ni se toman como valor del indicador ni entran en la
    # clave de agrupacion. Si entraran en la clave, cada sexo caeria en un grupo
    # distinto (0,0 vs 100,0) y la brecha no se calcularia.
    def _es_porcentaje(col):
        return "porcentaje" in col or col.startswith("pct") or "pct_" in col

    idx_ignorar = {i for i, col in enumerate(cabeceras_min) if _es_porcentaje(col)}

    # Elegimos como columna de valor la PRIMERA realmente numerica (evita
    # confundir una columna de texto como "Indicador" con el valor).
    idx_valor = None
    for i, col in enumerate(cabeceras_min):
        if col in ("año", "sexo", "territorio", "id") or i in idx_ignorar:
            continue
        try:
            float(next(f[i] for f in filas if f[i] is not None))
            idx_valor = i
            break
        except (StopIteration, ValueError, TypeError):
            continue
    if idx_valor is None:
        return ""

    nombre_valor = cabeceras[idx_valor]

    # Agrupamos por las dimensiones (todo lo que no sea Sexo, el valor ni una
    # columna de porcentaje cruda) para calcular la brecha de cada grupo por
    # separado (cada año, cada territorio).
    grupos = {}
    for fila in filas:
        clave = tuple(str(fila[i]) for i in range(len(cabeceras_min))
                      if i not in (idx_sexo, idx_valor) and i not in idx_ignorar)
        sexo_valor = str(fila[idx_sexo]).strip().lower()
        try:
            grupos.setdefault(clave, {})[sexo_valor] = float(fila[idx_valor])
        except (ValueError, TypeError):
            pass

    lineas = []
    for clave, sexos in grupos.items():
        if "hombre" not in sexos or "mujer" not in sexos:
            continue
        tipo = _tipo_indicador(nombre_valor, (sexos["hombre"], sexos["mujer"]))
        contexto = f"Para el grupo {list(clave)}: " if clave and any(clave) else ""
        lineas.append("- " + contexto + _frase_brecha(sexos["hombre"],
                                                       sexos["mujer"], tipo))
    return "\n".join(lineas)


def _redactar_respuesta(pregunta, sql, cabeceras, filas,
                        pide_anio=False, pide_evolucion=False):
    """
    Segunda llamada al modelo: redacta la respuesta en lenguaje natural.

    'pide_anio' y 'pide_evolucion' se calculan en responder() sobre TODO el
    hilo de conversacion (no solo sobre 'pregunta'), para que la red de
    seguridad del año respete la intencion aunque venga de un turno anterior
    (p. ej. una peticion de evolucion seguida de una aclaracion "Mujer").
    """
    cabeceras_min = [c.lower() for c in cabeceras]

    # -- FIX (bug 1 y 6): red de seguridad del "año por defecto" --------------
    # Si la pregunta no menciona un año ni pide la evolucion, pero el resultado
    # trae varios años, nos quedamos SOLO con el ultimo. Asi la brecha y el
    # desglose se calculan sobre el ultimo año aunque la SQL hubiera devuelto la
    # serie completa o un promedio multianual (corrige el caso "brecha salarial"
    # que daba 18,9 % en vez de 12,81 %). Cuando SI se pide evolucion, NO se
    # colapsa: se devuelve la serie completa.
    if "año" in cabeceras_min and not pide_anio and not pide_evolucion:
        ia = cabeceras_min.index("año")
        try:
            anio_max = max(int(f[ia]) for f in filas if f[ia] is not None)
            filas = [f for f in filas if f[ia] is not None and int(f[ia]) == anio_max]
        except (ValueError, TypeError):
            pass

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

    # -- FIX (bug 4 y 8): nota de año calculada en Python ---------------------
    # En vez de pedir al modelo que sustituya "(AAAA)", calculamos aqui los años
    # distintos del resultado. La coletilla "datos del año mas reciente" SOLO
    # tiene sentido cuando el usuario NO pidio un año y el resultado tiene un
    # unico año (comportamiento por defecto). Si el usuario pidio un año
    # explicito, o si hay varios años (evolucion), no se añade.
    instruccion_anio = ""
    if "año" in cabeceras_min and not pide_anio:
        ia = cabeceras_min.index("año")
        anios = sorted({str(f[ia]) for f in filas if f[ia] is not None})
        if len(anios) == 1:
            instruccion_anio = (
                "\nAl final de la respuesta, en un parrafo aparte y SIN ninguna "
                "etiqueta (no escribas 'Nota de año') ni comillas, escribe "
                f"exactamente esta frase: Los datos son del año mas reciente "
                f"disponible ({anios[0]}). Se puede pedir la evolucion completa "
                "de todos los años.\n")
        elif len(anios) > 1:
            instruccion_anio = (
                "\nINSTRUCCION INTERNA (no la copies en la respuesta): el "
                "resultado incluye varios años, asi que NO añadas ninguna "
                "coletilla sobre el 'año mas reciente disponible'.\n")

    # -- FIX (bug 2, 4, 5, 10): brecha calculada y redactada en Python --------
    # La brecha (con su unidad y su signo) se calcula aqui de forma exacta y se
    # pasa al modelo ya redactada, para que solo la transcriba.
    bloque_calculos_python = ""
    try:
        bloque_calculos_python = _calcular_brechas(sql, cabeceras,
                                                   cabeceras_min, filas)
    except Exception:
        # Seguridad pasiva: si algo falla analizando los datos, la app no se
        # bloquea; simplemente no se adjuntan brechas precalculadas.
        bloque_calculos_python = ""

    instruccion_calculos = ""
    if bloque_calculos_python:
        instruccion_calculos = (
            "\nFRASES DE BRECHA YA REDACTADAS POR EL SISTEMA (transcribelas tal "
            "cual, no recalcules ni reformules ni cambies el sentido):\n"
            f"{bloque_calculos_python}\n")

    respuesta = cliente.chat.completions.create(
        model=config.AI_MODEL,
        messages=[
            {"role": "system", "content": (
                "Redactas respuestas claras y breves en español a partir de "
                "resultados de consultas SQL. No inventes datos. Presenta todos "
                "los datos del resultado, no solo una parte.\n\n"
                "PERSPECTIVA DE GÉNERO (cuando el resultado incluya datos de "
                "Hombre y de Mujer):\n"
                "1. Presenta la respuesta SEPARADA en dos bloques claramente "
                "diferenciados: primero los datos de Hombres y despues los de "
                "Mujeres. No los mezcles en una sola lista.\n"
                "2. A continuacion, SOLO si el sistema te ha proporcionado una o "
                "varias 'FRASES DE BRECHA YA REDACTADAS', añade un apartado de "
                "brecha de genero TRANSCRIBIENDO esas frases tal cual, una por "
                "grupo. Si el sistema NO te proporciona ninguna frase de brecha, "
                "NO calcules ni inventes ninguna brecha: limitate a presentar "
                "los datos de cada bloque.\n"
                "3. No restes ni dividas tu por tu cuenta los valores de las "
                "filas: usa exclusivamente las frases que te da el sistema. "
                "Ignora cualquier columna de porcentaje por sexo que venga en "
                "las filas crudas (como 'Porcentaje_Mujeres'): no la muestres.\n"
                "4. Si hay varios años o categorias, presenta el desglose y la "
                "brecha correspondiente de cada uno."
            )},
            {"role": "user", "content": (
                f"Pregunta del usuario: {pregunta}\n\n"
                f"Consulta ejecutada: {sql}\n\n"
                f"Resultado ({len(filas)} filas):\n{tabla_texto}{aviso}\n"
                f"{instruccion_calculos}"
                f"{instruccion_anio}\n"
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
    recuerde mensajes anteriores (por ejemplo, una aclaracion de territorio o
    el indicador de la pregunta anterior para seguimientos tipo "¿y en 2019?").

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

    # -- FIX (bug 1, comparacion): intencion calculada sobre TODO el hilo ------
    # Se mira el texto de todos los turnos de usuario + la pregunta actual, no
    # solo la pregunta, para que "evolucion" o "comparar territorios" sigan
    # detectandose aunque la pregunta actual sea una aclaracion corta ("Mujer").
    texto_hilo = _texto_conversacion(historial, pregunta)
    pide_evolucion = any(p in texto_hilo for p in _PALABRAS_EVOLUCION)
    pide_anio = bool(re.search(r"\b(19|20)\d{2}\b", texto_hilo))
    compara_territorio = any(p in texto_hilo
                             for p in _PALABRAS_COMPARA_TERRITORIO)

    # Construimos los mensajes: sistema + turnos previos + pregunta actual.
    mensajes = [{"role": "system",
                 "content": _prompt_sistema(esquema, diccionario, territorios)}]
    mensajes += historial
    mensajes.append({"role": "user", "content": pregunta})

    # -- FIX (bug 1 y comparacion): directivas deterministas para esta pregunta
    # Reforzamos en la generacion de SQL lo que ya detectamos por reglas, para
    # que el modelo no colapse la serie ni pida territorio cuando hay que
    # comparar. Esto NO depende de que el modelo "acierte" a interpretar la
    # intencion: se la imponemos.
    directivas = []
    if pide_evolucion:
        directivas.append(
            "La pregunta pide la EVOLUCION temporal: NO filtres por un unico "
            "año ni uses WHERE Año = (SELECT MAX(Año)...). Devuelve TODOS los "
            "años disponibles, con la columna Año en el SELECT y ORDER BY Año.")
    if compara_territorio:
        directivas.append(
            "La pregunta COMPARA territorios: NO pidas aclaracion de territorio "
            "y NO filtres por un unico territorio. Incluye Territorio en SELECT "
            "y GROUP BY y devuelve todos los territorios de la tabla. Si la "
            "tabla solo tiene un territorio o no tiene dimension territorial, "
            "responde que ese indicador no se desglosa por territorio.")
    if directivas:
        mensajes.append({"role": "system",
                         "content": "INSTRUCCIONES PARA ESTA PREGUNTA:\n- "
                                    + "\n- ".join(directivas)})

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

    texto = _redactar_respuesta(pregunta, sql, cabeceras, filas,
                                pide_anio=pide_anio,
                                pide_evolucion=pide_evolucion)

    # -- FIX (bug 6): historial conversacional ACOTADO ------------------------
    # Antes se reiniciaba el historial a [] tras cada respuesta, lo que rompia
    # los seguimientos multiturno ("¿y en 2019?", "¿y en Araba?"): el agente
    # perdia el indicador anterior. Ahora conservamos un historial corto: el
    # ultimo turno + una nota con la SQL ejecutada (que le da al modelo la
    # tabla y los filtros usados). Se limita a los ultimos intercambios para no
    # arrastrar contexto indefinidamente ni inflar los tokens.
    historial_actualizado = historial + [
        {"role": "user", "content": pregunta},
        {"role": "assistant", "content": f"(consulta ejecutada: {sql})"},
    ]
    MAX_MENSAJES = 4  # 2 intercambios (pregunta + nota) x 2
    historial_actualizado = historial_actualizado[-MAX_MENSAJES:]

    # Las filas se convierten a listas normales para que sean fáciles de usar
    # desde la interfaz web.
    return {"sql": sql, "respuesta": texto,
            "cabeceras": cabeceras,
            "filas": [list(f) for f in filas],
            "historial": historial_actualizado}
