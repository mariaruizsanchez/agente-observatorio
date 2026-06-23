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
import unicodedata

from openai import OpenAI
import config
import database


# Marcador de version. Sirve para confirmar, de un vistazo en la interfaz, que
# Streamlit esta sirviendo esta version y no una cacheada/antigua. Para mostrarlo
# en la app: st.caption(f"Agente v{agent.VERSION}") o similar.
VERSION = "2026-06-23m-herencia-eliptica"


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

# Tablas donde, ademas de no calcular brecha, NO se desglosa por sexo: la
# columna Sexo describe al sujeto (sexo del recien nacido) y la pregunta natural
# ("cuantos nacimientos") espera el TOTAL, no el reparto por sexo del bebe. Para
# estas tablas se suma sobre Sexo y se devuelve el total. (Demografia_Jovenes NO
# va aqui: ahi el desglose por sexo de la poblacion joven si es informativo.)
TABLAS_SIN_DESGLOSE_SEXO = ("Demografia_Nacimientos",)

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


# Frases que indican una peticion demasiado amplia para una sola consulta
# ("dame todos los datos", "que datos tienes"...). En vez de devolver algo
# arbitrario, el agente responde con las dimensiones disponibles e invita a
# concretar.
_PETICIONES_AMPLIAS = (
    "todos los datos", "todas las dimensiones", "todas las tablas",
    "todo lo que tienes", "todo lo que hay", "dame todo", "muestrame todo",
    "que datos tienes", "que datos hay", "que informacion tienes",
    "todas las variables", "todos los indicadores",
)


def _es_peticion_amplia(pregunta):
    """True si la pregunta pide 'todos los datos' de forma inespecifica."""
    q = "".join(c for c in unicodedata.normalize("NFKD", (pregunta or "").lower())
                if not unicodedata.combining(c))
    return any(p in q for p in _PETICIONES_AMPLIAS)


def _listar_dimensiones(esquema):
    """
    Extrae los nombres de tabla del texto del esquema (lineas 'Tabla X:') para
    ofrecerselas al usuario como dimensiones consultables. Limpia el prefijo de
    esquema (dbo.), descarta tablas de sistema (firewall, sysdiagrams) y las
    tablas de dimension de apoyo (Dim_Año, Dim_Sexo, Dim_Territorio), que no son
    indicadores consultables por si mismas.
    """
    tablas = re.findall(r"(?im)^\s*Tabla\s+([^\s:]+)\s*:", esquema or "")
    nombres = []
    for t in tablas:
        crudo = t.split(".")[-1]
        bajo = crudo.lower()
        if bajo.startswith("dim_") or bajo.startswith("dim ") or \
           "firewall" in bajo or "sysdiagram" in bajo or \
           bajo.startswith("database"):
            continue
        legible = crudo.replace("_", " ")
        if legible not in nombres:
            nombres.append(legible)
    return nombres


# Tabla y columna de salud mental que el modelo a veces olvida desglosar.
_TABLA_SALUD_MENTAL = "salud_mental"
_COL_GRUPO_EDAD = "Grupo_Edad"
_PALABRAS_SALUD_MENTAL = ("ansiedad", "depresion", "salud mental")


def _asegurar_grupo_edad(pregunta, sql):
    """
    FIX (Q15) determinista: en preguntas de salud mental (ansiedad / depresion),
    si la SQL consulta la tabla de salud mental pero NO selecciona Grupo_Edad,
    se lo añadimos al SELECT (y al GROUP BY si lo hay) ANTES de ejecutar. Asi el
    resultado trae una fila por tramo de edad y luego se etiqueta '15-24 años:...'
    en vez de varias filas indistinguibles. Es conservador: si la lista de
    columnas usa '*' (ya estan todas) o no se reconoce la estructura, no toca la
    SQL.
    """
    q = "".join(c for c in unicodedata.normalize("NFKD", (pregunta or "").lower())
                if not unicodedata.combining(c))
    if not any(p in q for p in _PALABRAS_SALUD_MENTAL):
        return sql
    low = sql.lower()
    if _TABLA_SALUD_MENTAL not in low or "grupo_edad" in low:
        return sql

    # Separamos: prefijo SELECT [DISTINCT][TOP n] | lista de columnas | FROM...
    m = re.match(
        r"(?is)^(\s*SELECT\s+(?:DISTINCT\s+)?(?:TOP\s+\d+\s+)?)(.*?)(\bFROM\b.*)$",
        sql)
    if not m:
        return sql
    lista_cols = m.group(2)
    if "*" in lista_cols:
        return sql  # ya incluye todas las columnas, Grupo_Edad entre ellas

    nuevo = m.group(1) + _COL_GRUPO_EDAD + ", " + lista_cols + m.group(3)
    # Si hay GROUP BY, añadimos tambien Grupo_Edad como primera clave.
    if re.search(r"(?is)\bGROUP\s+BY\b", nuevo):
        nuevo = re.sub(r"(?is)(\bGROUP\s+BY\s+)",
                       r"\1" + _COL_GRUPO_EDAD + ", ", nuevo, count=1)
    return nuevo


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
- Para entrecomillar identificadores usa corchetes [Columna], NUNCA comillas
  invertidas (backticks); SQL Server las rechaza.
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
  NO preguntes. Desglosa por territorio: INCLUYE la columna Territorio en el
  SELECT y en el GROUP BY y devuelve TODOS los territorios de esa tabla (una
  fila por territorio y sexo), ordenados por Territorio. NUNCA mezcles ni sumes
  varios territorios en un mismo valor, y NUNCA devuelvas filas por territorio
  sin la columna Territorio que las identifique.
Nunca ofrezcas ni uses un territorio que no aparezca en la lista para esa tabla.

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

REGLA DEL TIPO DE INDICADOR:
Algunas tablas guardan VARIOS indicadores distintos en una misma columna de
tipo (por ejemplo, una columna 'Tipo de Tasa' con los valores 'Tasa de paro',
'Tasa de actividad' y 'Tasa de ocupacion'). Cuando la pregunta se refiera a uno
concreto, FILTRA SIEMPRE por ese tipo en la clausula WHERE
(WHERE [Tipo de Tasa] = 'Tasa de actividad', por ejemplo) usando exactamente el
valor que figure en el diccionario. NUNCA devuelvas varios tipos mezclados: si
la pregunta dice "tasa de actividad", el resultado debe contener solo la tasa
de actividad, no tambien la de paro ni la de ocupacion.

REGLA DEL TIPO DE INDICADOR:
Algunas tablas guardan VARIOS indicadores en una misma columna de tipo (por
ejemplo, una columna "Tipo de Tasa" con valores "Tasa de paro", "Tasa de
actividad", "Tasa de ocupacion/empleo"). Si la pregunta nombra uno concreto
("tasa de actividad", "tasa de paro", "tasa de ocupacion"), DEBES filtrar por
ese tipo (WHERE "Tipo de Tasa" = '...') usando el valor exacto del diccionario.
NUNCA devuelvas varios tipos mezclados en la misma respuesta: cada fila debe
corresponder a un unico tipo, el que se ha pedido.

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

REGLA DE DIMENSION DE CATEGORIA (tramos de edad, niveles, categorias):
Si la tabla tiene una columna de categoria o tramo (por ejemplo un tramo de
edad, un nivel educativo o una categoria) y la consulta va a devolver una fila
por cada valor de esa categoria, INCLUYE SIEMPRE esa columna en el SELECT (y en
el GROUP BY), para que cada fila quede identificada por su categoria. NUNCA
devuelvas varias filas que se distinguen por una categoria sin incluir la
columna que las identifica: el resultado quedaria con valores indistinguibles.
"""


def _limpiar_sql(texto):
    """
    Limpia la respuesta del modelo hasta dejar SQL ejecutable.

    1. Quita los bloques de codigo ```sql ... ``` si el modelo los añade.
    2. FIX (regresion del historial): si el modelo antepone prosa o un envoltorio
       (por ejemplo, copia el formato "(consulta ejecutada: SELECT ...)" que
       aparecia en el historial), empezamos en el primer SELECT o WITH y
       descartamos lo anterior; luego retiramos el ")" / ";" sobrante del cierre.
       Esto NO debilita la seguridad: ejecutar_consulta sigue exigiendo SELECT
       como primera palabra y bloqueando los tokens de escritura.
    3. Tambien tolera CTEs de lectura (WITH ... SELECT ...) y comentarios
       iniciales.
    """
    sql = texto.strip()
    if sql.startswith("```"):
        sql = sql.split("```")[1]
        if sql.lower().startswith("sql"):
            sql = sql[3:]
    sql = sql.strip()

    # FIX (error de sintaxis por backticks): el modelo a veces usa comillas
    # invertidas (sintaxis de MySQL) para los identificadores, p. ej.
    # `Tabla`.`Columna`, que SQL Server rechaza con "Incorrect syntax near '`'".
    # Las convertimos a corchetes [ ] de SQL Server: `Columna` -> [Columna].
    if "`" in sql:
        sql = re.sub(r"`([^`]+)`", r"[\1]", sql)
        sql = sql.replace("`", "")  # por si queda alguna suelta

    # Empezamos en el primer SELECT o WITH (descarta prosa/envoltorio inicial).
    m = re.search(r"(?is)\b(SELECT|WITH)\b", sql)
    if m:
        sql = sql[m.start():]

    # Retiramos parentesis/punto y coma sobrantes del envoltorio en el cierre,
    # SIN tocar los parentesis legitimos (solo si quedan descompensados).
    sql = sql.strip()
    while sql and sql[-1] in ");":
        if sql[-1] == ")" and sql.count(")") <= sql.count("("):
            break  # parentesis equilibrado: forma parte de la consulta
        sql = sql[:-1].rstrip()
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
    """
    Extrae el nombre de la tabla principal de la consulta (clausula FROM).
    Captura el nombre COMPLETO, incluido el esquema si lo lleva (p. ej.
    'dbo.Demografia_Jovenes'). Antes la expresion se cortaba en el punto y solo
    devolvia 'dbo', con lo que la comprobacion de TABLAS_SIN_BRECHA fallaba y se
    calculaba brecha en tablas que debian quedar excluidas (jovenes, nacimientos).
    """
    m = re.search(r"\bFROM\s+([^\s,;()]+)", sql, re.IGNORECASE)
    return m.group(1).replace("[", "").replace("]", "") if m else ""


def _sin_acentos(s):
    """Minusculas y sin acentos, para comparar texto de forma robusta."""
    s = str(s).lower()
    return "".join(c for c in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(c))


def _colapsar_sexo(cabeceras, cabeceras_min, filas):
    """
    Para tablas donde Sexo describe al sujeto y no es una brecha (nacimientos),
    suma sobre Sexo y devuelve el TOTAL, quitando la columna Sexo. Asi
    "cuantos nacimientos en Bizkaia 2022" da 6861 en vez de partirlo por sexo del
    bebe. Es determinista: da igual que el modelo haya desglosado por sexo.
    """
    if "sexo" not in cabeceras_min:
        return cabeceras, filas
    idx_sexo = cabeceras_min.index("sexo")

    # Columna de valor: primera numerica que no sea año.
    idx_valor = None
    for i, c in enumerate(cabeceras_min):
        if c in ("año", "territorio", "sexo", "id"):
            continue
        try:
            float(next(f[i] for f in filas if f[i] is not None))
            idx_valor = i
            break
        except (StopIteration, ValueError, TypeError):
            continue

    if idx_valor is None:
        # Sin valor numerico claro: solo quitamos la columna Sexo.
        nuevas_cab = [c for i, c in enumerate(cabeceras) if i != idx_sexo]
        nuevas_filas = [tuple(v for i, v in enumerate(f) if i != idx_sexo)
                        for f in filas]
        return nuevas_cab, nuevas_filas

    # Agrupamos por todas las columnas menos Sexo y el valor, sumando el valor.
    otras = [i for i in range(len(cabeceras)) if i not in (idx_sexo, idx_valor)]
    grupos, orden = {}, []
    for f in filas:
        clave = tuple(f[i] for i in otras)
        if clave not in grupos:
            grupos[clave] = 0.0
            orden.append(clave)
        try:
            grupos[clave] += float(f[idx_valor])
        except (ValueError, TypeError):
            pass

    nuevas_cab = [cabeceras[i] for i in otras] + [cabeceras[idx_valor]]
    nuevas_filas = []
    for clave in orden:
        total = grupos[clave]
        total = int(total) if total == int(total) else total
        nuevas_filas.append(tuple(clave) + (total,))
    return nuevas_cab, nuevas_filas


def _escalar_salud_mental(sql, cabeceras, filas):
    """
    En consultas de salud mental, la columna de valor es una proporcion (0,268)
    que a veces llega SIN el prefijo 'Pct_' (el modelo la renombra con AS), por
    lo que _escalar_porcentajes no la pilla y se mostraria como 0,268 en vez de
    26,8%. Aqui, ACOTADO a la tabla de salud mental y por MAGNITUD, escalamos a %
    la columna numerica de valor si todos sus valores estan en [0, 1.5] (es una
    fraccion) y no esta ya marcada como porcentaje. Es seguro: solo actua en esa
    tabla y no toca recuentos (valores grandes) ni columnas ya escaladas.
    """
    if _TABLA_SALUD_MENTAL not in (sql or "").lower():
        return cabeceras, filas
    nuevas_cab = list(cabeceras)
    nuevas_filas = [list(f) for f in filas]
    for i, c in enumerate(cabeceras):
        bajo = str(c).lower()
        if bajo in ("año", "id") or "territorio" in bajo or "sexo" in bajo \
           or "edad" in bajo or "(%)" in bajo:
            continue
        valores = []
        for f in filas:
            try:
                valores.append(float(f[i]))
            except (ValueError, TypeError):
                valores = None
                break
        if not valores or any(abs(v) > 1.5 for v in valores):
            continue  # no es una fraccion (recuento, año...) -> no escalar
        for f in nuevas_filas:
            try:
                f[i] = round(float(f[i]) * 100, 1)
            except (ValueError, TypeError):
                pass
        if "%" not in nuevas_cab[i]:
            nuevas_cab[i] = f"{nuevas_cab[i]} (%)"
    return nuevas_cab, [tuple(f) for f in nuevas_filas]


def _columnas_dimension_texto(cabeceras, cabeceras_min, filas):
    """
    FIX (Q15): detecta columnas de CATEGORIA textual que distinguen unas filas de
    otras y que el modelo tiende a omitir al redactar (tramos de edad, categorias
    de salud, etc.). Se excluyen las dimensiones que ya se tratan aparte (año,
    territorio, sexo) y las columnas numericas (que son el valor del indicador).
    Devuelve los NOMBRES de columna, para nombrarselos explicitamente al modelo y
    obligarle a etiquetar cada fila (p. ej. '15-24 años: ...').
    """
    dims = []
    for i, cmin in enumerate(cabeceras_min):
        if cmin in ("año", "territorio", "sexo", "id"):
            continue
        valores = [f[i] for f in filas if f[i] is not None]
        if len(valores) < 2:
            continue
        # ¿Es texto (categoria) y toma varios valores distintos?
        es_texto = False
        for v in valores:
            try:
                float(v)
            except (ValueError, TypeError):
                es_texto = True
                break
        if es_texto and len({str(v) for v in valores}) > 1:
            dims.append(cabeceras[i])
    return dims


# Tipos de tasa que pueden convivir en una columna 'Tipo de Tasa' y las pistas
# de texto que los identifican en la pregunta.
_TIPOS_TASA = {
    "paro": ("paro", "desemple"),
    "actividad": ("actividad",),
    "ocupacion": ("ocupacion", "empleo"),
}


def _filtrar_tipo_tasa(pregunta, cabeceras, filas):
    """
    FIX (tasa de actividad): la tabla de tasas guarda varios indicadores
    ('Tasa de paro', 'Tasa de actividad', 'Tasa de ocupacion') en una columna
    'Tipo de Tasa'. Si el modelo no filtra por el tipo pedido, devuelve los tres
    mezclados y la brecha no se puede calcular. Aqui, de forma DETERMINISTA: si
    la pregunta menciona un unico tipo y el resultado trae la columna de tipo con
    varios valores, nos quedamos solo con las filas de ese tipo. No depende de
    que el modelo acierte.
    """
    q = _sin_acentos(pregunta or "")
    objetivo = None
    for clave, pistas in _TIPOS_TASA.items():
        if any(p in q for p in pistas):
            # Si la pregunta menciona mas de un tipo, no filtramos (ambiguo).
            objetivo = clave if objetivo is None else None
            if objetivo is None:
                return filas
    if objetivo is None:
        return filas

    # Columna de tipo: solo la especifica de tasas ('Tipo de Tasa'), para no
    # confundirla con otras columnas que lleven 'tipo' en el nombre.
    idx_tipo = next((i for i, c in enumerate(cabeceras)
                     if "tipo" in c.lower() and "tasa" in c.lower()), None)
    if idx_tipo is None:
        return filas

    valores = {_sin_acentos(f[idx_tipo]) for f in filas if f[idx_tipo] is not None}
    if len(valores) <= 1:
        return filas  # ya viene un unico tipo, no hace falta filtrar

    filtradas = [f for f in filas if f[idx_tipo] is not None
                 and objetivo in _sin_acentos(f[idx_tipo])]
    return filtradas if filtradas else filas


# Territorios y sus variantes (sin acentos) para detectarlos en la pregunta.
_TERRITORIOS_PISTAS = {
    "araba": ("araba", "alava"),
    "bizkaia": ("bizkaia", "vizcaya"),
    "gipuzkoa": ("gipuzkoa", "guipuzcoa"),
}


def _territorios_en_texto(texto):
    """Conjunto de territorios (claves) mencionados en un texto."""
    t = _sin_acentos(texto or "")
    return {clave for clave, pistas in _TERRITORIOS_PISTAS.items()
            if any(p in t for p in pistas)}


def _es_seguimiento_eliptico(pregunta):
    """
    True si la pregunta es un seguimiento eliptico que se apoya en el turno
    anterior ("¿y en 2019?", "¿y para mujeres?", "2019") y NO una pregunta nueva
    y completa ("¿Cuál es el salario medio?"). Solo en estos seguimientos se
    hereda el territorio del contexto; asi una pregunta nueva sin territorio NO
    arrastra el territorio de una pregunta previa.
    """
    q = _sin_acentos(pregunta or "").strip().strip("¿?¡!.,").strip()
    if not q:
        return False
    palabras = q.split()
    if palabras[0] == "y":                     # "y en 2019", "y para mujeres"
        return True
    if len(palabras) <= 3 and re.search(r"(19|20)\d{2}", q):  # "en 2019", "2019"
        return True
    return False


def _filtrar_a_territorio(cabeceras, filas, territorio):
    """
    Deja solo las filas de 'territorio' si el resultado trae varios. Respaldo
    determinista del seguimiento conversacional ("Araba" -> "¿y en 2019?"): si la
    pregunta actual no nombra territorio pero el contexto fijo uno, se mantiene.
    """
    cabm = [c.lower() for c in cabeceras]
    if "territorio" not in cabm:
        return filas
    idx = cabm.index("territorio")
    objetivo = _sin_acentos(territorio)
    valores = {_sin_acentos(f[idx]) for f in filas if f[idx] is not None}
    if len(valores) <= 1:
        return filas
    filtradas = [f for f in filas if f[idx] is not None
                 and objetivo in _sin_acentos(f[idx])]
    return filtradas if filtradas else filas


def _filtrar_territorio_pedido(pregunta, cabeceras, filas):
    """
    FIX (evolucion en un territorio concreto): si la pregunta menciona UN solo
    territorio (p. ej. "...en Bizkaia") y el resultado trae varios, dejamos solo
    las filas de ese territorio. Asi la brecha no incluye territorios que no se
    han pedido (caso de la evolucion del paro en Bizkaia, donde la brecha salia
    de los tres territorios). Si menciona varios (comparacion) o ninguno, no
    filtra: se respeta el desglose por territorio.
    """
    q = _sin_acentos(pregunta or "")
    objetivo = None
    for clave, pistas in _TERRITORIOS_PISTAS.items():
        if any(p in q for p in pistas):
            objetivo = clave if objetivo is None else None
            if objetivo is None:
                return filas  # varios territorios mencionados -> comparacion
    if objetivo is None:
        return filas

    cabm = [c.lower() for c in cabeceras]
    if "territorio" not in cabm:
        return filas
    idx = cabm.index("territorio")
    valores = {_sin_acentos(f[idx]) for f in filas if f[idx] is not None}
    if len(valores) <= 1:
        return filas
    filtradas = [f for f in filas if f[idx] is not None
                 and objetivo in _sin_acentos(f[idx])]
    return filtradas if filtradas else filas


def _escalar_porcentajes(cabeceras, filas):
    """
    Las columnas cuyo nombre empieza por 'Pct_' (o 'Pct ') guardan la tasa como
    FRACCION (0,605). Para la respuesta escrita las multiplicamos por 100 y
    marcamos la columna como porcentaje, de modo que se muestren como 60,5 en vez
    de 0,605. La brecha en puntos porcentuales sigue saliendo igual (el calculo
    detecta la escala). Solo escala valores en rango de fraccion (<=1,5) para no
    multiplicar dos veces si ya vinieran en %.
    """
    idx_pct = [i for i, c in enumerate(cabeceras)
               if c.lower().startswith("pct_") or c.lower().startswith("pct ")]
    if not idx_pct:
        return cabeceras, filas

    nuevas_cab = list(cabeceras)
    for i in idx_pct:
        if "%" not in nuevas_cab[i]:
            nuevas_cab[i] = nuevas_cab[i] + " (%)"

    nuevas_filas = []
    for f in filas:
        f = list(f)
        for i in idx_pct:
            try:
                v = float(f[i])
                if abs(v) <= 1.5:
                    f[i] = round(v * 100, 1)
            except (ValueError, TypeError):
                pass
        nuevas_filas.append(tuple(f))
    return nuevas_cab, nuevas_filas


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

    # FIX (bug 4, afinado): columnas de PORCENTAJE POR SEXO precalculadas que el
    # modelo pudiera colar (p. ej. "Porcentaje_Mujeres" con 0/100 por sexo). Se
    # ignoran por completo: ni se toman como valor ni entran en la clave de
    # agrupacion. IMPORTANTE: solo se descartan si el nombre referencia un sexo;
    # asi NO se confunden con columnas de valor legitimas como "Pct_Tasa", que
    # son el propio indicador (una tasa) y deben usarse para calcular la brecha.
    def _es_porcentaje_por_sexo(col):
        es_pct = "porcentaje" in col or col.startswith("pct") or "pct_" in col
        ref_sexo = "mujer" in col or "hombre" in col or "sexo" in col
        return es_pct and ref_sexo

    idx_ignorar = {i for i, col in enumerate(cabeceras_min)
                   if _es_porcentaje_por_sexo(col)}

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
    # columna de porcentaje por sexo). Guardamos los valores en LISTAS para
    # detectar colisiones: si una misma combinacion (grupo, sexo) aparece varias
    # veces, es que falta una dimension en la salida (tipicamente Territorio,
    # cuando el modelo devuelve varias filas sin la columna Territorio). Antes se
    # sobrescribia y la brecha se calculaba sobre una fila al azar (la ultima).
    grupos = {}
    for fila in filas:
        clave = tuple(str(fila[i]) for i in range(len(cabeceras_min))
                      if i not in (idx_sexo, idx_valor) and i not in idx_ignorar)
        sexo_valor = str(fila[idx_sexo]).strip().lower()
        try:
            grupos.setdefault(clave, {}).setdefault(sexo_valor, []).append(
                float(fila[idx_valor]))
        except (ValueError, TypeError):
            pass

    lineas = []
    for clave, sexos in grupos.items():
        if "hombre" not in sexos or "mujer" not in sexos:
            continue
        hay_colision = len(sexos["hombre"]) > 1 or len(sexos["mujer"]) > 1
        tipo = _tipo_indicador(nombre_valor,
                               (sexos["hombre"][0], sexos["mujer"][0]))
        if hay_colision and tipo != "recuento":
            # Faltan dimensiones y NO se pueden agregar tasas ni importes con
            # fiabilidad (no se promedian tasas de varios territorios). Para no
            # dar un numero enganoso, se omite la brecha de este grupo.
            continue
        # En recuentos, varias filas del mismo grupo (p. ej. territorios) se SUMAN
        # para dar el total; en tasas/importes hay un unico valor por grupo.
        val_h = sum(sexos["hombre"]) if tipo == "recuento" else sexos["hombre"][0]
        val_m = sum(sexos["mujer"]) if tipo == "recuento" else sexos["mujer"][0]
        contexto = f"Para el grupo {list(clave)}: " if clave and any(clave) else ""
        lineas.append("- " + contexto + _frase_brecha(val_h, val_m, tipo))
    return "\n".join(lineas)


def _redactar_respuesta(pregunta, sql, cabeceras, filas,
                        pide_anio=False, pide_evolucion=False,
                        compara_territorio=False, territorio_contexto=None):
    """
    Segunda llamada al modelo: redacta la respuesta en lenguaje natural.

    'pide_anio' y 'pide_evolucion' se calculan en responder() sobre TODO el
    hilo de conversacion (no solo sobre 'pregunta'), para que la red de
    seguridad del año respete la intencion aunque venga de un turno anterior
    (p. ej. una peticion de evolucion seguida de una aclaracion "Mujer").
    """
    cabeceras_min = [c.lower() for c in cabeceras]

    # -- FIX (nacimientos, decision (b)): colapsar el desglose por sexo --------
    # En las tablas donde el sexo describe al sujeto (nacimientos), sumamos sobre
    # Sexo y devolvemos el total, sin partir por sexo del bebe.
    tabla_actual = _tabla_de_sql(sql)
    if any(t.lower() in tabla_actual.lower() for t in TABLAS_SIN_DESGLOSE_SEXO):
        cabeceras, filas = _colapsar_sexo(cabeceras, cabeceras_min, filas)
        cabeceras_min = [c.lower() for c in cabeceras]

    # -- FIX (tasa de actividad): filtro determinista por tipo de tasa --------
    # Si la pregunta pide un tipo concreto (paro/actividad/ocupacion) y el
    # resultado trae varios tipos mezclados, dejamos solo las filas de ese tipo
    # ANTES de calcular nada. Asi la brecha se calcula sobre el indicador pedido
    # y no se omite por colision de tipos.
    filas = _filtrar_tipo_tasa(pregunta, cabeceras, filas)

    # -- FIX (Q4): filtrar al territorio pedido si la pregunta menciona uno ----
    # Si la pregunta dice "...en Bizkaia" pero el resultado trae varios
    # territorios, dejamos solo ese, para que la brecha no liste los tres.
    filas = _filtrar_territorio_pedido(pregunta, cabeceras, filas)

    # -- FIX (Q19): respaldo del territorio heredado del contexto -------------
    # Si la pregunta no nombra territorio pero la conversacion fijo uno
    # ("Araba" -> "¿y en 2019?"), nos quedamos con ese (por si el modelo
    # desglosa por los tres pese a la directiva).
    if territorio_contexto:
        filas = _filtrar_a_territorio(cabeceras, filas, territorio_contexto)

    # -- Peticion: columnas 'Pct_' se muestran como porcentaje (x100) ---------
    cabeceras, filas = _escalar_porcentajes(cabeceras, filas)
    # En salud mental la proporcion a veces llega sin prefijo Pct_; la escalamos
    # por magnitud (acotado a esa tabla) para que tambien se muestre como %.
    cabeceras, filas = _escalar_salud_mental(sql, cabeceras, filas)
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
        # El bloque de brecha NO se le pide al modelo (lo añadimos nosotros tras
        # su respuesta). Solo le avisamos de que no lo calcule el, para que no
        # invente una brecha en % relativo cuando debe ser en puntos
        # porcentuales (era el caso de la evolucion de la tasa de paro).
        instruccion_calculos = ""

    # -- FIX (Q15): etiquetado explicito de columnas de categoria -------------
    # Si el resultado trae columnas de categoria textual (tramos de edad, etc.),
    # se las nombramos al modelo para que etiquete cada fila con su valor en vez
    # de soltar cifras sueltas. Nombrar la columna concreta es mucho mas fiable
    # que la regla generica.
    instruccion_dimension = ""
    dims = _columnas_dimension_texto(cabeceras, cabeceras_min, filas)
    if dims:
        nombres = ", ".join(f"'{d}'" for d in dims)
        instruccion_dimension = (
            f"\nIMPORTANTE: cada fila se distingue por el valor de la columna "
            f"{nombres}. Escribe ese valor DELANTE de cada cifra (por ejemplo, "
            f"'15-24 años: 0,27'). No omitas esa columna ni presentes las cifras "
            f"sueltas sin indicar a que categoria corresponde cada una.\n")

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
                "2. NO escribas ninguna frase de brecha, diferencia, comparacion "
                "ni porcentaje entre hombres y mujeres: el sistema añade ese "
                "apartado automaticamente despues de tu respuesta. Tu limitate a "
                "los dos bloques de datos.\n"
                "3. No restes, dividas ni calcules nada por tu cuenta con los "
                "valores de las filas. Ignora cualquier columna de porcentaje "
                "por sexo que venga en las filas crudas (como 'Porcentaje_"
                "Mujeres'): no la muestres.\n"
                "4. Si hay varios años, tramos de edad, categorias u otra "
                "dimension que distinga unas filas de otras, presenta el "
                "desglose IDENTIFICANDO cada fila por esa dimension (por "
                "ejemplo: '15-24 años: ...', '25-44 años: ...'). NUNCA devuelvas "
                "varios valores sueltos sin indicar a que corresponde cada uno. "
                "Muestra todas las columnas que distingan las filas."
            )},
            {"role": "user", "content": (
                f"Pregunta del usuario: {pregunta}\n\n"
                f"Consulta ejecutada: {sql}\n\n"
                f"Resultado ({len(filas)} filas):\n{tabla_texto}{aviso}\n"
                f"{instruccion_calculos}"
                f"{instruccion_dimension}"
                f"{instruccion_anio}\n"
                "Redacta la respuesta para el usuario."
            )},
        ],
        temperature=0.2,
    )
    texto = respuesta.choices[0].message.content.strip()

    # -- Q27: comparacion territorial imposible (agregado de Euskadi) ---------
    # Si la pregunta pide comparar territorios pero el resultado tiene un unico
    # territorio (o ninguno), avisamos de forma determinista de que el indicador
    # no se desglosa por territorio, en vez de devolver el agregado como si
    # respondiera a "en que territorio es mayor...".
    if compara_territorio:
        territorios_distintos = set()
        if "territorio" in cabeceras_min:
            it = cabeceras_min.index("territorio")
            territorios_distintos = {str(f[it]).strip() for f in filas
                                     if f[it] is not None}
        if len(territorios_distintos) <= 1:
            nota = ("Nota: este indicador es un agregado de C.A. de Euskadi y no "
                    "se desglosa por territorio, asi que no es posible compararlo "
                    "entre Araba, Bizkaia y Gipuzkoa. Se muestra el dato global.")
            texto = nota + "\n\n" + texto

    # -- Brecha DETERMINISTA: la añade Python, no el modelo -------------------
    # Asi el tipo de brecha (pp / % + diferencia / recuento + proporcion) y el
    # signo son siempre exactos, sin depender de que el modelo transcriba bien.
    # Se inserta antes de la coletilla de año si esta existe en el texto.
    if bloque_calculos_python:
        apartado = "Brecha de género:\n" + bloque_calculos_python
        marca = "Los datos son del año"
        if marca in texto:
            cabeza, _, cola = texto.partition(marca)
            texto = cabeza.rstrip() + "\n\n" + apartado + "\n\n" + marca + cola
        else:
            texto = texto.rstrip() + "\n\n" + apartado
    return texto


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

    # -- FIX (Q22): peticion demasiado amplia ("dame todos los datos") --------
    # No se mapea a una sola consulta util; respondemos con las dimensiones
    # disponibles e invitamos a concretar, en vez de devolver un dato arbitrario.
    if _es_peticion_amplia(pregunta):
        dims = _listar_dimensiones(esquema)
        if dims:
            lista = "\n".join(f"- {d}" for d in dims)
            respuesta = (
                "Tu pregunta es demasiado amplia para una sola consulta. Puedo "
                "responder sobre indicadores concretos de estas dimensiones:\n\n"
                f"{lista}\n\n"
                "Concreta qué indicador te interesa (y, si quieres, territorio, "
                "año o sexo). Por ejemplo: \"tasa de paro en Bizkaia\" o "
                "\"brecha salarial\".")
        else:
            respuesta = (
                "Tu pregunta es demasiado amplia para una sola consulta. "
                "Concreta qué indicador, territorio o año te interesa.")
        nuevo_historial = (historial + [
            {"role": "user", "content": pregunta},
            {"role": "assistant", "content": respuesta},
        ])[-4:]
        return {"sql": None, "respuesta": respuesta,
                "cabeceras": None, "filas": None,
                "historial": nuevo_historial}

    # -- FIX (bug 1, comparacion): intencion calculada con cuidado -----------
    # La intencion (evolucion / comparacion / año) se toma de la PREGUNTA
    # ACTUAL. Solo cuando la pregunta actual es la respuesta a una aclaracion
    # (el ultimo turno del asistente fue un "ACLARAR:") miramos tambien los
    # turnos previos del hilo, para no perder un "evolucion" que estaba en la
    # pregunta original antes de la aclaracion (caso "evolucion ... -> Mujer").
    # Asi se evita que una pregunta nueva herede por error el "evolucion" de
    # una pregunta anterior ya respondida que quedo en el historial.
    en_aclaracion = bool(historial) and \
        str(historial[-1].get("role", "")) == "assistant" and \
        str(historial[-1].get("content", "")).strip().upper().startswith("ACLARAR")
    if en_aclaracion:
        texto_intencion = _texto_conversacion(historial, pregunta)
    else:
        texto_intencion = (pregunta or "").lower()
    pide_evolucion = any(p in texto_intencion for p in _PALABRAS_EVOLUCION)
    # El año se detecta aunque lleve un sufijo pegado, como en euskera
    # ("2022an", "2020ko"). Por eso NO se exige \b despues de las cifras; basta
    # con que no haya otro digito justo antes ni despues (evita capturar dentro
    # de numeros mas largos como 12022 o 20225).
    pide_anio = bool(re.search(r"(?<!\d)(19|20)\d{2}(?!\d)", texto_intencion))
    compara_territorio = any(p in texto_intencion
                             for p in _PALABRAS_COMPARA_TERRITORIO)

    # FIX (Q19): territorio heredado del contexto, SOLO en seguimientos
    # elipticos ("Araba" -> "¿y en 2019?"). Si la pregunta actual es nueva y
    # completa (p. ej. "¿Cuál es el salario medio?"), NO se hereda el territorio
    # de una pregunta anterior, aunque aquella mencionara uno.
    territorio_contexto = None
    if (_es_seguimiento_eliptico(pregunta)
            and not _territorios_en_texto(pregunta)
            and not compara_territorio):
        for m in reversed(historial):
            if m.get("role") == "user":
                previos = _territorios_en_texto(m.get("content", ""))
                if len(previos) == 1:
                    territorio_contexto = next(iter(previos)).capitalize()
                break

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
    directivas.append(
        "Desglose por sexo: comprueba en el ESQUEMA si la tabla que vas a "
        "consultar tiene una columna Sexo. SOLO si la tiene, por defecto "
        "incluyela en el SELECT y el GROUP BY para separar Hombre y Mujer (no "
        "los mezcles en un AVG o SUM conjunto): por ejemplo, 'salario medio' "
        "debe darse separado por sexo. EXCEPCIONES: (a) si la pregunta pide "
        "datos de UN SOLO sexo ('mujeres en la industria', 'paro femenino', "
        "'que porcentaje de mujeres tiene...'), filtra por ese sexo con "
        "WHERE Sexo = 'Mujer' (o 'Hombre') y devuelve solo ese sexo; (b) si la "
        "pregunta pide que PROPORCION de un total es de un sexo ('que porcentaje "
        "de las personas electas son mujeres'), incluye AMBOS sexos para poder "
        "calcular el total. Si la tabla NO tiene columna Sexo (por ejemplo la de "
        "nacimientos), NO añadas Sexo: provocaria el error 'Invalid column "
        "name'. Nunca uses una columna que no exista en el esquema de esa tabla.")
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
    if not pide_evolucion and not pide_anio:
        # Caso por defecto: ningun año mencionado y no se pide evolucion. Se
        # fuerza el ultimo año disponible y se MANTIENE la columna Año en el
        # SELECT. Esto evita que el modelo agregue varios años en un total (lo
        # que ocurria, p. ej., con las excedencias, que sumaban toda la serie).
        directivas.append(
            "No se menciona ningun año: devuelve SOLO el ultimo año disponible "
            "con WHERE Año = (SELECT MAX(Año) FROM <la misma tabla>) e INCLUYE "
            "la columna Año en el SELECT. No sumes ni promedies varios años.")
    if not compara_territorio:
        if territorio_contexto:
            # Seguimiento que hereda territorio: mantenerlo, no desglosar.
            directivas.append(
                f"La conversacion ya fijo el territorio {territorio_contexto}. "
                f"Filtra por WHERE Territorio = '{territorio_contexto}' y responde "
                "SOLO de ese territorio; NO desgloses por los demas.")
        else:
            # Recordatorio del territorio (comportamiento (b), elegido por el
            # usuario): sin territorio especificado se DESGLOSA por territorio, no
            # se pregunta. Esto ademas evita que el modelo devuelva varias filas
            # (una por territorio) SIN la columna Territorio, que mezclaba datos.
            directivas.append(
                "Respeta la REGLA DEL TERRITORIO: si la tabla tiene varios "
                "territorios y la conversacion no indica cual, NO preguntes; "
                "desglosa por territorio incluyendo SIEMPRE la columna Territorio "
                "en el SELECT y el GROUP BY, con una fila por territorio. No "
                "mezcles ni sumes territorios.")
    # FIX (Q15, acotado): la directiva de "incluir la columna de dimension" solo
    # se aplica a preguntas de salud / tramos de edad. Si se aplicaba siempre,
    # para tablas como accidentes empujaba a incluir TODOS los territorios
    # (rompiendo el seguimiento "Araba" -> "¿y en 2019?") o columnas agregadas
    # redundantes (rompiendo el % de electas). Por eso se condiciona y se excluye
    # explicitamente Año/Territorio/Sexo.
    q_norm = _sin_acentos(pregunta or "")
    if any(p in q_norm for p in ("ansiedad", "depresion", "salud mental",
                                 "tramo de edad", "grupo de edad",
                                 "por edad", "tramos de edad")):
        directivas.append(
            "Si la tabla de salud mental tiene la columna Grupo_Edad, INCLUYELA "
            "en el SELECT y el GROUP BY para que cada fila quede identificada por "
            "su tramo de edad. No añadas columnas agregadas redundantes ni "
            "cambies el desglose por territorio o sexo.")
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

    # Caso 1-bis: el modelo NO devolvio SQL, sino una respuesta en lenguaje
    # natural (pregunta fuera de dominio, dato inexistente, etc.). Tras limpiar,
    # una consulta valida debe empezar por SELECT o WITH; si no, es texto y lo
    # devolvemos como respuesta en vez de mandarlo al guard (que daria el
    # mensaje enganoso de "Solo se permiten consultas SELECT"). Asi una pregunta
    # como "capital de Francia" recibe una respuesta clara, no un error tecnico.
    primera = (sql.lstrip("(").strip().split(None, 1)[0].upper()
               if sql.strip() else "")
    if primera not in ("SELECT", "WITH"):
        nuevo_historial = (historial + [
            {"role": "user", "content": pregunta},
            {"role": "assistant", "content": sql},
        ])[-4:]
        return {"sql": None, "respuesta": sql,
                "cabeceras": None, "filas": None,
                "historial": nuevo_historial}

    # Caso 2: el modelo genero SQL. Lo ejecutamos.
    # Red determinista Q15: garantizamos el desglose por Grupo_Edad en salud mental.
    sql = _asegurar_grupo_edad(pregunta, sql)
    try:
        cabeceras, filas = database.ejecutar_consulta(sql)
    except Exception as e:
        # -- Red de reintento ante errores de SQL del motor -------------------
        # Si el error es de ejecucion del SQL (columna inexistente, sintaxis...)
        # y no del guard de solo lectura, reintentamos UNA vez devolviendo el
        # error al modelo para que corrija la consulta. Esto recupera, por
        # ejemplo, el caso en que el modelo añade una columna Sexo a una tabla
        # que no la tiene ('Invalid column name').
        mensaje_error = str(e)
        es_error_motor = any(p in mensaje_error.lower() for p in
                             ("invalid column", "incorrect syntax",
                              "invalid object", "ambiguous column"))
        if es_error_motor and _reintentos < 1:
            correccion = mensajes + [
                {"role": "assistant", "content": sql},
                {"role": "user", "content": (
                    f"La consulta anterior fallo con este error de SQL Server: "
                    f"{mensaje_error}\nCorrigela usando UNICAMENTE columnas que "
                    f"existan en el esquema de esa tabla y la sintaxis de SQL "
                    f"Server. Devuelve solo la consulta SELECT corregida.")},
            ]
            sql_corregida = _pedir_sql(correccion)
            try:
                cabeceras, filas = database.ejecutar_consulta(sql_corregida)
                sql = sql_corregida
            except Exception as e2:
                return {"sql": sql_corregida,
                        "respuesta": f"No se pudo ejecutar la consulta: {e2}",
                        "cabeceras": None, "filas": None, "historial": historial}
        else:
            return {"sql": sql,
                    "respuesta": f"No se pudo ejecutar la consulta: {e}",
                    "cabeceras": None, "filas": None, "historial": historial}

    texto = _redactar_respuesta(pregunta, sql, cabeceras, filas,
                                pide_anio=pide_anio,
                                pide_evolucion=pide_evolucion,
                                compara_territorio=compara_territorio,
                                territorio_contexto=territorio_contexto)

    # -- FIX (bug 6, corregido): historial conversacional ACOTADO -------------
    # Antes se reiniciaba el historial a [] tras cada respuesta, lo que rompia
    # los seguimientos multiturno ("¿y en 2019?", "¿y en Araba?"). La primera
    # version de este arreglo guardaba la SQL como turno del asistente, pero el
    # modelo IMITABA ese formato y devolvia su SQL envuelta en
    # "(consulta ejecutada: ...)", que el guard rechazaba. Ahora guardamos la
    # RESPUESTA EN LENGUAJE NATURAL (recortada) como turno del asistente: es un
    # historial conversacional normal, da contexto suficiente para los
    # seguimientos (menciona el indicador y el territorio) y el modelo no lo
    # confunde con SQL. Se limita a los ultimos intercambios.
    nota_asistente = texto if len(texto) <= 600 else texto[:600] + "…"
    historial_actualizado = historial + [
        {"role": "user", "content": pregunta},
        {"role": "assistant", "content": nota_asistente},
    ]
    MAX_MENSAJES = 4  # 2 intercambios
    historial_actualizado = historial_actualizado[-MAX_MENSAJES:]

    # Las filas se convierten a listas normales para que sean fáciles de usar
    # desde la interfaz web.
    return {"sql": sql, "respuesta": texto,
            "cabeceras": cabeceras,
            "filas": [list(f) for f in filas],
            "historial": historial_actualizado}
