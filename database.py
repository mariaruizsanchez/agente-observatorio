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
    """
    if not os.path.exists(RUTA_EXCEL):
        return ""

    df = pd.read_excel(RUTA_EXCEL, sheet_name="_Diccionario")
    lineas = []
    for _, fila in df.iterrows():
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
    datos reales.
    """
    consulta_tablas = """
        SELECT TABLE_NAME FROM INFORMATION_SCHEMA.COLUMNS
        WHERE COLUMN_NAME = 'Territorio'
    """
    lineas = []
    mapa = {}
    with conectar() as conn:
        cursor = conn.cursor()
        cursor.execute(consulta_tablas)
        tablas = [fila[0] for fila in cursor.fetchall()]

        for tabla in sorted(tablas):
            cursor.execute(
                f"SELECT DISTINCT Territorio FROM [{tabla}] "
                f"WHERE Territorio IS NOT NULL"
            )
            valores = cursor.fetchall()
            territorios = sorted(v[0] for v in valores)
            mapa[tabla] = territorios
            lineas.append(f"{tabla}: {', '.join(territorios)}")

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
    if primera_palabra != "SELECT":
        raise ValueError("Solo se permiten consultas SELECT de lectura.")
    if any(p in sql_limpio.upper().split() for p in prohibidas):
        raise ValueError("La consulta contiene instrucciones no permitidas.")

    with conectar() as conn:
        cursor = conn.cursor()
        cursor.execute(sql_limpio)
        cabeceras = [d[0] for d in cursor.description]
        filas = cursor.fetchall()
    return cabeceras, filas
