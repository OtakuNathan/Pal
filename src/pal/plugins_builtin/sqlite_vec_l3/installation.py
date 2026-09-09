def verify(context):
    import sqlite3
    import sqlite_vec

    connection = sqlite3.connect(":memory:")
    try:
        connection.enable_load_extension(True)
        sqlite_vec.load(connection)
        version = connection.execute("select vec_version()").fetchone()[0]
        return {"ok": True, "sqlite_vec_version": version}
    finally:
        connection.close()
