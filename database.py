"""
Acceso a la base de datos Azure SQL.

Responsabilidades:
  1. Leer el esquema (tablas y columnas) para dárselo al modelo como contexto.
  2. Leer el diccionario de datos (descripciones y valores posibles de cada
     columna) para que el modelo no tenga que adivinar los valores.
  3. Ejecutar las consultas SELECT que el modelo genera, de forma segura.
"""

import os
import pyodbc
import pandas as pd
import config

# Ruta del Excel, de donde se lee la hoja _Diccionario.
RUTA_EXCEL = r"..\Observatorio_SocioLaboral_Datos.xlsx"


def conectar():
    """Abre una conexión con la base de datos Azure SQL."""
    return pyodbc.connect(config.DB_CONNECTION_STRING)


def obtener_esquema():
    """
    Devuelve un texto con todas las tablas y sus columnas.

    Este texto se inyecta en el prompt del sistema para que el modelo
    sepa qué tablas y columnas existen y genere SQL correcto. Por eso es
    tan importante que las tablas y columnas tengan nombres descriptivos.
    """
    consulta = """
        SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE
        FROM INFORMATION_SCHEMA.COLUMNS
        ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION
    """
    with conectar() as conn:
        filas = conn.cursor().execute(consulta).fetchall()

    # Agrupamos las columnas por tabla
    tablas = {}
    for esquema, tabla, columna, tipo in filas:
        nombre_completo = f"{esquema}.{tabla}"
        tablas.setdefault(nombre_completo, []).append(f"{columna} ({tipo})")

    # Lo formateamos en un texto legible para el modelo
    lineas = []
    for tabla, columnas in tablas.items():
        lineas.append(f"Tabla {tabla}:")
        for col in columnas:
            lineas.append(f"  - {col}")
        lineas.append("")
    return "\n".join(lineas)


def obtener_diccionario():
    """
    Devuelve un texto con el diccionario de datos: la descripción y los
    valores posibles de cada columna, leídos de la hoja _Diccionario del Excel.

    Esto es clave para la calidad del Text-to-SQL: sin esta información el
    modelo adivina los valores (escribe 'Paro' cuando en los datos pone
    'Tasa de paro'). Con el diccionario, usa los valores reales.

    Si el Excel no está disponible, devuelve cadena vacía y el agente sigue
    funcionando solo con el esquema.
    """
    if not os.path.exists(RUTA_EXCEL):
        return ""

    df = pd.read_excel(RUTA_EXCEL, sheet_name="_Diccionario")
    lineas = []
    for _, fila in df.iterrows():
        partes = [f"{fila['Tabla']}.{fila['Columna']}"]
        if pd.notna(fila.get("Descripcion")):
            partes.append(f"descripción: {fila['Descripcion']}")
        if pd.notna(fila.get("Valores_Posibles")):
            partes.append(f"valores posibles: {fila['Valores_Posibles']}")
        lineas.append(" | ".join(partes))
    return "\n".join(lineas)


def obtener_territorios():
    """
    Devuelve (texto, mapa) con los territorios de cada tabla.

    - texto: descripción legible para incluir en el prompt del modelo.
    - mapa: diccionario {nombre_tabla: [lista de territorios]}, que el código
      usa para resolver automáticamente las tablas de un solo territorio.

    Se consulta directamente la base de datos, así que refleja siempre los
    datos reales (no una lista escrita a mano).
    """
    consulta_tablas = """
        SELECT TABLE_NAME FROM INFORMATION_SCHEMA.COLUMNS
        WHERE COLUMN_NAME = 'Territorio'
    """
    lineas = []
    mapa = {}
    with conectar() as conn:
        cursor = conn.cursor()
        tablas = [fila[0] for fila in cursor.execute(consulta_tablas).fetchall()]

        for tabla in sorted(tablas):
            valores = cursor.execute(
                f"SELECT DISTINCT Territorio FROM [{tabla}] "
                f"WHERE Territorio IS NOT NULL"
            ).fetchall()
            territorios = sorted(v[0] for v in valores)
            mapa[tabla] = territorios
            lineas.append(f"{tabla}: {', '.join(territorios)}")

    return "\n".join(lineas), mapa


def ejecutar_consulta(sql):
    """
    Ejecuta una consulta SELECT y devuelve (cabeceras, filas).

    Por seguridad solo se permiten consultas de lectura: si el SQL generado
    contiene una instrucción de escritura, se rechaza antes de ejecutar.
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
        cursor = conn.cursor().execute(sql_limpio)
        cabeceras = [d[0] for d in cursor.description]
        filas = cursor.fetchall()
    return cabeceras, filas
