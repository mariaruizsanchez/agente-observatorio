"""
Acceso a la base de datos Azure SQL.

Responsabilidades:
  1. Leer el esquema (tablas y columnas) para darselo al modelo como contexto.
  2. Leer el diccionario de datos (descripciones y valores posibles de cada
     columna) para que el modelo no tenga que adivinar los valores.
  3. Ejecutar las consultas SELECT que el modelo genera, de forma segura.

La conexion se realiza con la libreria pymssql, que se comunica con SQL Server
sin necesidad de un controlador ODBC instalado en el sistema operativo. Esto
permite que el mismo codigo funcione tanto en local como en un servicio de
alojamiento en la nube (Streamlit Community Cloud).
"""

import os
import pymssql
import pandas as pd
import config

# Ruta del Excel del que se lee la hoja _Diccionario.
# Se busca primero junto al codigo (caso de la app desplegada en la nube,
# donde el Excel se incluye en el repositorio) y, si no, en la carpeta
# superior (caso del ordenador local, donde el Excel esta fuera del proyecto).
_AQUI = os.path.dirname(os.path.abspath(__file__))
_RUTA_LOCAL = os.path.join(_AQUI, "Observatorio_SocioLaboral_Datos.xlsx")
_RUTA_SUPERIOR = os.path.join(_AQUI, "..", "Observatorio_SocioLaboral_Datos.xlsx")
RUTA_EXCEL = _RUTA_LOCAL if os.path.exists(_RUTA_LOCAL) else _RUTA_SUPERIOR


def conectar():
    """
    Abre una conexion con la base de datos Azure SQL.

    Los datos de conexion (servidor, usuario, contrase\u00f1a y base de datos)
    se obtienen de la configuracion (config.py), que a su vez los lee del
    archivo .env en local o de los secrets de Streamlit en la nube.
    """
    return pymssql.connect(
        server=config.DB_SERVER,
        user=config.DB_USER,
        password=config.DB_PASSWORD,
        database=config.DB_NAME,
        port=1433,
    )


def obtener_esquema():
    """
    Devuelve un texto con todas las tablas y sus columnas.

    Este texto se incluye en el mensaje de sistema para que el modelo sepa
    que tablas y columnas existen y genere SQL correcto.
    """
    consulta = """
        SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE
        FROM INFORMATION_SCHEMA.COLUMNS
        ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION
    """
    with conectar() as conn:
        cursor = conn.cursor()
        cursor.execute(consulta)
        filas = cursor.fetchall()

    tablas = {}
    for esquema, tabla, columna, tipo in filas:
        nombre_completo = f"{esquema}.{tabla}"
        tablas.setdefault(nombre_completo, []).append(f"{columna} ({tipo})")

    lineas = []
    for tabla, columnas in tablas.items():
        lineas.append(f"Tabla {tabla}:")
        for col in columnas:
            lineas.append(f"  - {col}")
        lineas.append("")
    return "\n".join(lineas)


def obtener_diccionario():
    """
    Devuelve un texto con el diccionario de datos: la descripcion y los
    valores posibles de cada columna, leidos de la hoja _Diccionario del Excel.

    Si el Excel no esta disponible, devuelve cadena vacia y el agente sigue
    funcionando solo con el esquema.

    FIX (bug 7): NO se incluyen aqui los valores posibles de la columna
    Territorio. La unica fuente de verdad de los territorios es la consulta en
    vivo a la BBDD (obtener_territorios), que refleja los datos reales de cada
    tabla. De este modo el diccionario estatico ya no puede afirmar un
    territorio que la tabla no tiene (p. ej. 'C.A. de Euskadi' en Trabajo_Tasas);
    esa contradiccion era la que hacia que el modelo filtrara por un territorio
    inexistente y la consulta devolviera resultados vacios.
    """
    if not os.path.exists(RUTA_EXCEL):
        return ""

    df = pd.read_excel(RUTA_EXCEL, sheet_name="_Diccionario")
    lineas = []
    for _, fila in df.iterrows():
        # Saltamos las filas que describen la columna Territorio: los
        # territorios reales se aportan, ya filtrados por tabla, desde
        # obtener_territorios().
        if str(fila.get("Columna", "")).strip().lower() == "territorio":
            continue

        partes = [f"{fila['Tabla']}.{fila['Columna']}"]
        if pd.notna(fila.get("Descripcion")):
            partes.append(f"descripcion: {fila['Descripcion']}")
        if pd.notna(fila.get("Valores_Posibles")):
            partes.append(f"valores posibles: {fila['Valores_Posibles']}")
        lineas.append(" | ".join(partes))
    return "\n".join(lineas)


def obtener_territorios():
    """
    Devuelve (texto, mapa) con los territorios de cada tabla.

    - texto: descripcion legible para incluir en el mensaje de sistema.
    - mapa: diccionario {nombre_tabla: [lista de territorios]}, que el codigo
      usa para resolver automaticamente las tablas de un solo territorio.

    Se consulta directamente la base de datos, asi que refleja siempre los
    datos reales. Esta es la fuente de verdad UNICA de los territorios (ver la
    nota en obtener_diccionario).

    FIX (bug salario + territorio): ademas de las tablas CON territorio, se
    calculan las tablas SIN columna Territorio (indicadores agregados de
    Euskadi, como los salarios) y se listan explicitamente en el texto. Asi el
    modelo sabe que para esas tablas NO debe pedir territorio ni filtrar por el,
    lo que evitaba que "salario de mujeres vs hombres" pidiera territorio y
    luego devolviera vacio al filtrar por Bizkaia.
    """
    consulta_tablas = """
        SELECT TABLE_NAME FROM INFORMATION_SCHEMA.COLUMNS
        WHERE COLUMN_NAME = 'Territorio'
    """
    consulta_todas = """
        SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_TYPE = 'BASE TABLE'
    """
    lineas = []
    mapa = {}
    with conectar() as conn:
        cursor = conn.cursor()

        cursor.execute(consulta_tablas)
        tablas_con_territorio = sorted({fila[0] for fila in cursor.fetchall()})

        cursor.execute(consulta_todas)
        todas_las_tablas = {fila[0] for fila in cursor.fetchall()}

        for tabla in tablas_con_territorio:
            cursor.execute(
                f"SELECT DISTINCT Territorio FROM [{tabla}] "
                f"WHERE Territorio IS NOT NULL"
            )
            valores = cursor.fetchall()
            territorios = sorted(v[0] for v in valores)
            mapa[tabla] = territorios
            lineas.append(f"{tabla}: {', '.join(territorios)}")

    # Tablas sin dimension territorial: todas las que existen menos las que
    # tienen columna Territorio (descartando las de sistema de SQL Server).
    sin_territorio = sorted(
        t for t in (todas_las_tablas - set(mapa.keys()))
        if not t.startswith("sys")
    )
    if sin_territorio:
        lineas.append("")
        lineas.append(
            "Tablas SIN dimension territorial (no tienen columna Territorio; "
            "son agregados que NO se desglosan por territorio: para ellas nunca "
            "pidas territorio ni añadas WHERE Territorio): "
            + ", ".join(sin_territorio)
        )

    return "\n".join(lineas), mapa


def ejecutar_consulta(sql):
    """
    Ejecuta una consulta SELECT y devuelve (cabeceras, filas).

    Por seguridad solo se permiten consultas de lectura: si el SQL generado
    contiene una instruccion de escritura, se rechaza antes de ejecutar.
    """
    sql_limpio = sql.strip().rstrip(";")
    prohibidas = ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER",
                  "CREATE", "TRUNCATE", "MERGE", "EXEC")
    primera_palabra = sql_limpio.upper().split(None, 1)[0] if sql_limpio else ""
    # Se permiten consultas de lectura que empiezan por SELECT o por WITH (CTE
    # de lectura). El bloqueo real de escritura lo hace el escaneo de tokens de
    # abajo (INSERT/UPDATE/DELETE/...), que tambien cubre el caso
    # "WITH ... INSERT ...".
    if primera_palabra not in ("SELECT", "WITH"):
        raise ValueError("Solo se permiten consultas SELECT de lectura.")
    if any(p in sql_limpio.upper().split() for p in prohibidas):
        raise ValueError("La consulta contiene instrucciones no permitidas.")

    with conectar() as conn:
        cursor = conn.cursor()
        cursor.execute(sql_limpio)
        cabeceras = [d[0] for d in cursor.description]
        filas = cursor.fetchall()
    return cabeceras, filas
