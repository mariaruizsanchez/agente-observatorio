"""
Carga inicial de datos: Excel  ->  Azure SQL.

Lee el archivo Observatorio_SocioLaboral_Datos.xlsx, crea una tabla en la base
de datos por cada hoja de datos, y vuelca todas las filas.

Se ejecuta UNA SOLA VEZ, después de haber creado la base de datos en Azure y
de haber rellenado el archivo .env con la cadena de conexión.

Uso:
    python cargar_datos.py
"""

import pandas as pd
import pyodbc
import config

# Ruta del Excel. Por defecto lo busca en la carpeta superior del proyecto;
# si lo tienes en otro sitio, cambia esta línea.
RUTA_EXCEL = r"..\Observatorio_SocioLaboral_Datos.xlsx"

# Hojas que NO son tablas de datos (documentación). No se cargan.
HOJAS_IGNORADAS = {"_Instrucciones", "_Diccionario"}

# Correspondencia entre tipos de pandas y tipos de SQL Server.
TIPOS_SQL = {
    "int64": "BIGINT",
    "float64": "FLOAT",
    "object": "NVARCHAR(255)",
    "bool": "BIT",
    "datetime64[ns]": "DATETIME2",
}


def tipo_sql(dtype):
    """Traduce un tipo de columna de pandas al tipo equivalente de SQL Server."""
    return TIPOS_SQL.get(str(dtype), "NVARCHAR(255)")


def nombre_columna(col):
    """Encierra el nombre de columna en corchetes (admite acentos y espacios)."""
    return "[" + str(col).replace("]", "") + "]"


def crear_y_cargar(conn, nombre_hoja, df):
    """Crea la tabla correspondiente a una hoja y carga sus filas."""
    cursor = conn.cursor()

    # 1. Si la tabla ya existía de un intento anterior, se elimina y se rehace.
    cursor.execute(f"IF OBJECT_ID('{nombre_hoja}', 'U') IS NOT NULL "
                   f"DROP TABLE [{nombre_hoja}]")

    # 2. CREATE TABLE con una columna por cada columna del Excel.
    columnas_sql = ", ".join(
        f"{nombre_columna(c)} {tipo_sql(df[c].dtype)}" for c in df.columns
    )
    cursor.execute(f"CREATE TABLE [{nombre_hoja}] ({columnas_sql})")

    # 3. INSERT de todas las filas.
    marcadores = ", ".join("?" for _ in df.columns)
    cols = ", ".join(nombre_columna(c) for c in df.columns)
    insert = f"INSERT INTO [{nombre_hoja}] ({cols}) VALUES ({marcadores})"

    # fast_executemany acelera mucho la carga de muchas filas.
    cursor.fast_executemany = True
    # Convertimos los valores nulos de pandas (NaN) a None para SQL.
    filas = [tuple(None if pd.isna(v) else v for v in fila)
             for fila in df.itertuples(index=False, name=None)]
    cursor.executemany(insert, filas)

    conn.commit()
    print(f"  OK  {nombre_hoja}: {len(filas)} filas cargadas")


def main():
    config.validar()

    print("Leyendo el Excel...")
    hojas = pd.read_excel(RUTA_EXCEL, sheet_name=None)

    print("Conectando a Azure SQL...\n")
    with pyodbc.connect(config.DB_CONNECTION_STRING) as conn:
        for nombre, df in hojas.items():
            if nombre in HOJAS_IGNORADAS:
                print(f"  --  {nombre}: hoja de documentación, se omite")
                continue
            crear_y_cargar(conn, nombre, df)

    print("\nCarga completada. La base de datos ya tiene todas las tablas.")


if __name__ == "__main__":
    main()
