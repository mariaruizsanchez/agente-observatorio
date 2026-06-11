"""
Carga inicial y sincronización de datos: Excel  ->  Azure SQL.

Lee el archivo Observatorio_SocioLaboral_Datos.xlsx, crea una tabla en la base
de datos por cada hoja de datos, y vuelca todas las filas.

Además, detecta si hay tablas en la base de datos que ya no existen como 
pestañas en el Excel (tablas fantasma) y las elimina automáticamente para
mantener la sincronización exacta.
"""

import os
import pandas as pd
import pymssql
import config

# Ruta del Excel: se busca primero junto al código (carpeta del proyecto) y,
# si no, en la carpeta superior.
_AQUI = os.path.dirname(os.path.abspath(__file__))
_RUTA_LOCAL = os.path.join(_AQUI, "Observatorio_SocioLaboral_Datos.xlsx")
_RUTA_SUPERIOR = os.path.join(_AQUI, "..", "Observatorio_SocioLaboral_Datos.xlsx")
RUTA_EXCEL = _RUTA_LOCAL if os.path.exists(_RUTA_LOCAL) else _RUTA_SUPERIOR

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


def conectar():
    """Abre una conexión con la base de datos Azure SQL."""
    return pymssql.connect(
        server=config.DB_SERVER,
        user=config.DB_USER,
        password=config.DB_PASSWORD,
        database=config.DB_NAME,
        port=1433,
    )


def crear_y_cargar(conn, nombre_hoja, df):
    """Crea la tabla correspondiente a una hoja y carga sus filas."""
    cursor = conn.cursor()

    # 1. Si la tabla ya existía de una carga anterior, se elimina y se rehace.
    cursor.execute(f"IF OBJECT_ID('{nombre_hoja}', 'U') IS NOT NULL "
                   f"DROP TABLE [{nombre_hoja}]")

    # 2. CREATE TABLE con una columna por cada columna del Excel.
    columnas_sql = ", ".join(
        f"{nombre_columna(c)} {tipo_sql(df[c].dtype)}" for c in df.columns
    )
    cursor.execute(f"CREATE TABLE [{nombre_hoja}] ({columnas_sql})")

    # 3. INSERT de todas las filas.
    marcadores = ", ".join("%s" for _ in df.columns)
    cols = ", ".join(nombre_columna(c) for c in df.columns)
    insert = f"INSERT INTO [{nombre_hoja}] ({cols}) VALUES ({marcadores})"

    # Convertimos los valores nulos de pandas (NaN) a None para SQL.
    filas = [tuple(None if pd.isna(v) else v for v in fila)
             for fila in df.itertuples(index=False, name=None)]
    cursor.executemany(insert, filas)

    conn.commit()
    print(f"  OK  {nombre_hoja}: {len(filas)} filas cargadas")


def limpiar_tablas_fantasma(conn, hojas_excel):
    """Detecta y elimina tablas en Azure SQL que ya no están en el Excel."""
    cursor = conn.cursor()
    
    # Obtenemos las hojas válidas que se van a cargar (ignorando documentación)
    hojas_validas = {nombre for nombre in hojas_excel.keys() if nombre not in HOJAS_IGNORADAS}
    
    # Consultamos las tablas que existen actualmente en Azure SQL
    cursor.execute("SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_TYPE = 'BASE TABLE'")
    tablas_existentes = {fila[0] for fila in cursor.fetchall()}
    
    # Encontramos las tablas fantasma (están en BD pero no en Excel)
    tablas_fantasma = tablas_existentes - hojas_validas
    
    # Eliminamos las tablas huérfanas
    for tabla in tablas_fantasma:
        # Medida de seguridad extra para no borrar tablas de sistema internas de SQL
        if not tabla.startswith("sys"): 
            print(f"  --  Eliminando tabla fantasma: {tabla}")
            cursor.execute(f"DROP TABLE [{tabla}]")
            
    conn.commit()


def main():
    config.validar()

    print("Leyendo el Excel...")
    hojas = pd.read_excel(RUTA_EXCEL, sheet_name=None)

    print("Conectando a Azure SQL...\n")
    with conectar() as conn:
        # Paso NUEVO: Limpiar la base de datos antes de cargar nada
        limpiar_tablas_fantasma(conn, hojas)

        # Paso HABITUAL: Cargar los datos nuevos
        for nombre, df in hojas.items():
            if nombre in HOJAS_IGNORADAS:
                print(f"  --  {nombre}: hoja de documentación, se omite")
                continue
            crear_y_cargar(conn, nombre, df)

    print("\nCarga completada. La base de datos es ahora un reflejo exacto del Excel.")


if __name__ == "__main__":
    main()
