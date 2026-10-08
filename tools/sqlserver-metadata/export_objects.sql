/*==============================================================================
 export_objects.sql: object definition harvest  (SQL Server 2017+)

 Writes one CSV row per procedure, view and table across every online user
 database on the instance. build_metadata.py turns that CSV into the folder
 corpus that check_queries.py --metadata reads.

 The table-DDL synthesis uses STRING_AGG, which is 2017 and later. Everything
 else runs on 2016; on a true 2016 instance swap that one aggregate for
 FOR XML PATH('') + STUFF.

 WHAT IT EMITS
 -------------
 One row per object, in the seven-column shape build_metadata.py expects (it
 reads by POSITION, so the order below is a contract):

     0  DatabaseName      e.g. Shop
     1  ObjectType        PROC | TABLE | VIEW      (the loader knows no others)
     2  SchemaName        e.g. dbo
     3  ObjectName        e.g. usp_refresh_sales
     4  ObjectTimestamp   sys.objects.modify_date as epoch seconds
     5  Definition        the object's SQL, newlines replaced by _LINEBREAK_
     6  Extra             always NULL/empty, reserved, the loader ignores it

 Procedures and views carry their real definition from sys.sql_modules.
 Tables have no stored definition, so a CREATE TABLE is SYNTHESISED from
 sys.columns: column list, types and nullability only, which is all the
 column checks need. Constraints, indexes, defaults and identity are
 deliberately not emitted.

 EVERY newline becomes the literal token _LINEBREAK_, so one object is always
 exactly one line of output. That is what makes a plain CSV dump safe.

 HOW TO GET IT OFF THE SERVER
 ----------------------------
 Preferred: bcp, because it has no length limit.

   1. Run this script with @PersistTable = 1 (the default). It leaves a
      permanent table behind (dbo.ObjectExport in the current database) with a
      ready-made CsvLine column. A permanent table is required because bcp
      connects as a separate session, and a ##global temp table dies with the
      session that made it.

   2. In a Command Prompt (not PowerShell; quoting differs):

        bcp "SELECT CsvLine FROM <YourDb>.dbo.ObjectExport ORDER BY DatabaseName, ObjectType, SchemaName, ObjectName" queryout objects.csv -S <server> -T -c -C 65001

      -C 65001 writes UTF-8 (the loader opens the file as utf-8-sig).

      DO NOT add -t"" -r"\n". cmd.exe mangles that pair: the closing quote of
      -r"\n" leaks into the row terminator, so every row after the first
      begins with a stray " and the first CSV field parses as a database name
      with a quote glued on. With one column and no -t, bcp appends its
      default tab terminator, which lands harmlessly in the ignored 7th field.
      A file with the fault is repairable: definitions contain no CR or LF, so
      \r\n" occurs ONLY as a mangled row terminator and can be replaced with
      \r\n throughout.

   3. Copy objects.csv over, then:
        python3 build_metadata.py --csv objects.csv --out corpus/

 Fallback: SSMS "Save Results As CSV" on the second result set. BEWARE: SSMS
 grid results are capped at 65,535 characters per cell and truncate SILENTLY.
 Large procedures exceed that easily. The run-info result set reports how
 many objects exceed the cap; if it is not zero, do not use this path.

 PERMISSIONS
 -----------
 Needs VIEW DEFINITION on each database (definitions come back NULL without
 it, indistinguishable at a glance from an encrypted object; both are counted
 separately in the run info). @PersistTable also needs CREATE TABLE in the
 current database; set it to 0 if that is refused and use the grid.

 PARAMETERS are below and are meant to be edited.
==============================================================================*/

-- Run from the database that should hold dbo.ObjectExport (USE <YourDb>;).
SET NOCOUNT ON;

--------------------------------------------------------------------------------
-- PARAMETERS
--------------------------------------------------------------------------------
-- 1 = drop dev/SIT/UAT/test databases from the harvest (patterns below).
DECLARE @ExcludeNonProdDatabases BIT = 1;

-- What to harvest. The loader only understands PROC/TABLE/VIEW.
DECLARE @IncludeProcedures BIT = 1;
DECLARE @IncludeViews      BIT = 1;
DECLARE @IncludeTables     BIT = 1;

-- Scalar/inline/table-valued functions. Emitted as type PROC so they land in
-- the PROCEDURE folder and get parsed like any other module. Off by default;
-- the column checks only read tables and views.
DECLARE @IncludeFunctions  BIT = 0;

-- 1 = also leave a permanent table behind for bcp. See header.
DECLARE @PersistTable      BIT = 1;
DECLARE @PersistTableName  nvarchar(256) = N'dbo.ObjectExport';

DECLARE @SnapshotDT datetime = GETDATE();

-- SSMS grid cell cap; used only to warn, nothing is truncated by this script.
DECLARE @GridCellLimit int = 65535;

--------------------------------------------------------------------------------
-- CLEANUP
--------------------------------------------------------------------------------
IF OBJECT_ID('tempdb..#DbFilter')     IS NOT NULL DROP TABLE #DbFilter;
IF OBJECT_ID('tempdb..#DbExcluded')   IS NOT NULL DROP TABLE #DbExcluded;
IF OBJECT_ID('tempdb..#ObjectExport') IS NOT NULL DROP TABLE #ObjectExport;
IF OBJECT_ID('tempdb..#Unreadable')   IS NOT NULL DROP TABLE #Unreadable;

--------------------------------------------------------------------------------
-- NON-PRODUCTION DATABASE FILTER
--   Patterns resolved once to concrete names, so every later check is a cheap
--   name comparison. Review before trusting a run: SELECT * FROM #DbExcluded;
--------------------------------------------------------------------------------
CREATE TABLE #DbFilter (Pattern nvarchar(128) NOT NULL);

INSERT INTO #DbFilter (Pattern) VALUES
    (N'%[_]dev%'),
    (N'%[_]sit%'),
    (N'%uat%'),         -- no underscore: catches "fooUAT" as well as "foo_uat"
    (N'%[_]test%'),
    (N'%[_]tst%'),
    (N'%[_]snapshot');  -- database snapshots duplicate every object

CREATE TABLE #DbExcluded (DatabaseName sysname NOT NULL PRIMARY KEY);

IF @ExcludeNonProdDatabases = 1
    INSERT INTO #DbExcluded (DatabaseName)
    SELECT DISTINCT d.name
    FROM sys.databases d
    WHERE d.database_id > 4
      AND EXISTS (SELECT 1 FROM #DbFilter f WHERE LOWER(d.name) LIKE f.Pattern);

--------------------------------------------------------------------------------
-- OUTPUT SHAPE
--   Definition is held with REAL newlines here and flattened to _LINEBREAK_ in
--   one place at the end, so the marker never has to be threaded through the
--   DDL synthesis.
--------------------------------------------------------------------------------
CREATE TABLE #ObjectExport
(
    DatabaseName    sysname       NOT NULL,
    ObjectType      varchar(8)    NOT NULL,   -- PROC | TABLE | VIEW
    SchemaName      sysname       NOT NULL,
    ObjectName      sysname       NOT NULL,
    ObjectTimestamp bigint        NULL,       -- epoch seconds of modify_date
    Definition      nvarchar(MAX) NULL
);

-- Objects whose text could not be read: encrypted, or VIEW DEFINITION denied.
-- Kept apart rather than emitted as empty bodies, which the parser would
-- silently accept as "a procedure that does nothing".
CREATE TABLE #Unreadable
(
    DatabaseName sysname     NOT NULL,
    SchemaName   sysname     NOT NULL,
    ObjectName   sysname     NOT NULL,
    ObjectType   varchar(8)  NOT NULL,
    Reason       varchar(32) NOT NULL     -- encrypted | no-definition
);

--------------------------------------------------------------------------------
-- HARVEST: one pass per online user database
--------------------------------------------------------------------------------
DECLARE @db sysname, @sql nvarchar(MAX);

DECLARE curDBs CURSOR LOCAL FAST_FORWARD FOR
    SELECT name
    FROM sys.databases
    WHERE database_id > 4            -- skip master, tempdb, model, msdb
      AND state_desc = 'ONLINE'
      AND HAS_DBACCESS(name) = 1
      AND NOT EXISTS (SELECT 1 FROM #DbExcluded x WHERE x.DatabaseName = name);

OPEN curDBs;
FETCH NEXT FROM curDBs INTO @db;

WHILE @@FETCH_STATUS = 0
BEGIN
    IF DB_ID(@db) IS NOT NULL
    BEGIN
        ----------------------------------------------------------------------
        -- MODULES: procedures, views, optionally functions
        ----------------------------------------------------------------------
        SET @sql = N'
            SET NOCOUNT ON;

            INSERT INTO #ObjectExport
                (DatabaseName, ObjectType, SchemaName, ObjectName, ObjectTimestamp, Definition)
            SELECT
                @dbParam,
                CASE WHEN o.type = ''V'' THEN ''VIEW'' ELSE ''PROC'' END,
                s.name,
                o.name,
                DATEDIFF_BIG(SECOND, ''1970-01-01'', o.modify_date),
                sm.definition
            FROM ' + QUOTENAME(@db) + N'.sys.objects AS o
            JOIN ' + QUOTENAME(@db) + N'.sys.schemas AS s
              ON s.schema_id = o.schema_id
            JOIN ' + QUOTENAME(@db) + N'.sys.sql_modules AS sm
              ON sm.object_id = o.object_id
            WHERE o.is_ms_shipped = 0
              AND s.name <> ''sys''
              AND sm.definition IS NOT NULL
              AND (
                    (o.type = ''P''  AND @procs = 1)
                 OR (o.type = ''V''  AND @views = 1)
                 OR (o.type IN (''FN'',''IF'',''TF'') AND @funcs = 1)
                  );

            -- Same population, but the text came back NULL (encrypted, or no
            -- VIEW DEFINITION). Recorded, never emitted as an empty body.
            INSERT INTO #Unreadable
                (DatabaseName, SchemaName, ObjectName, ObjectType, Reason)
            SELECT
                @dbParam, s.name, o.name,
                CASE WHEN o.type = ''V'' THEN ''VIEW'' ELSE ''PROC'' END,
                -- an encrypted module still HAS a sys.sql_modules row, just a
                -- NULL definition; no row at all means the text was never
                -- there to read. OBJECTPROPERTY is not used here on purpose —
                -- it resolves against the CURRENT database, not @dbParam.
                CASE WHEN sm.object_id IS NOT NULL THEN ''encrypted'' ELSE ''no-definition'' END
            FROM ' + QUOTENAME(@db) + N'.sys.objects AS o
            JOIN ' + QUOTENAME(@db) + N'.sys.schemas AS s
              ON s.schema_id = o.schema_id
            LEFT JOIN ' + QUOTENAME(@db) + N'.sys.sql_modules AS sm
              ON sm.object_id = o.object_id
            WHERE o.is_ms_shipped = 0
              AND s.name <> ''sys''
              AND sm.definition IS NULL
              AND (
                    (o.type = ''P''  AND @procs = 1)
                 OR (o.type = ''V''  AND @views = 1)
                 OR (o.type IN (''FN'',''IF'',''TF'') AND @funcs = 1)
                  );
        ';

        BEGIN TRY
            EXEC sys.sp_executesql @sql,
                 N'@dbParam sysname, @procs bit, @views bit, @funcs bit',
                 @dbParam = @db, @procs = @IncludeProcedures,
                 @views = @IncludeViews, @funcs = @IncludeFunctions;
        END TRY
        BEGIN CATCH
            PRINT 'Modules skipped for [' + @db + ']: ' + ERROR_MESSAGE();
        END CATCH

        ----------------------------------------------------------------------
        -- TABLES: synthesise CREATE TABLE from the catalogue
        --   STRING_AGG for the column list. The first fragment is CAST to
        --   nvarchar(max) on purpose: STRING_AGG over nvarchar(<=4000) inputs
        --   returns nvarchar(4000) and ERRORS at 8000 bytes ("STRING_AGG
        --   aggregation result exceeded the limit"), which the widest tables
        --   here would hit. One CAST makes the whole aggregate max-typed.
        ----------------------------------------------------------------------
        IF @IncludeTables = 1
        BEGIN
            SET @sql = N'
            SET NOCOUNT ON;

            INSERT INTO #ObjectExport
                (DatabaseName, ObjectType, SchemaName, ObjectName, ObjectTimestamp, Definition)
            SELECT
                @dbParam,
                ''TABLE'',
                s.name,
                t.name,
                DATEDIFF_BIG(SECOND, ''1970-01-01'', t.modify_date),
                N''CREATE TABLE '' + QUOTENAME(s.name) + N''.'' + QUOTENAME(t.name)
                  + CHAR(13) + CHAR(10) + N''('' + CHAR(13) + CHAR(10)
                  + (
                        SELECT STRING_AGG(
                            CAST(N''    '' + QUOTENAME(c.name) + N'' '' AS nvarchar(max)) +
                            CASE
                                -- computed column: keep the expression
                                WHEN cc.definition IS NOT NULL
                                    THEN N''AS '' + cc.definition
                                ELSE
                                    UPPER(ty.name) +
                                    CASE
                                        WHEN ty.name IN (''char'',''varchar'',''binary'',''varbinary'')
                                            THEN N''('' + CASE WHEN c.max_length = -1 THEN N''MAX''
                                                            ELSE CAST(c.max_length AS nvarchar(10)) END + N'')''
                                        WHEN ty.name IN (''nchar'',''nvarchar'')
                                            THEN N''('' + CASE WHEN c.max_length = -1 THEN N''MAX''
                                                            ELSE CAST(c.max_length / 2 AS nvarchar(10)) END + N'')''
                                        WHEN ty.name IN (''decimal'',''numeric'')
                                            THEN N''('' + CAST(c.precision AS nvarchar(10)) + N'','' +
                                                          CAST(c.scale AS nvarchar(10)) + N'')''
                                        WHEN ty.name IN (''datetime2'',''time'',''datetimeoffset'')
                                            THEN N''('' + CAST(c.scale AS nvarchar(10)) + N'')''
                                        ELSE N''''
                                    END
                                    + CASE WHEN c.is_nullable = 1 THEN N'' NULL'' ELSE N'' NOT NULL'' END
                            END,
                            N'','' + CHAR(13) + CHAR(10))
                            WITHIN GROUP (ORDER BY c.column_id)
                        FROM ' + QUOTENAME(@db) + N'.sys.columns AS c
                        JOIN ' + QUOTENAME(@db) + N'.sys.types AS ty
                          ON ty.user_type_id = c.user_type_id
                        LEFT JOIN ' + QUOTENAME(@db) + N'.sys.computed_columns AS cc
                          ON cc.object_id = c.object_id AND cc.column_id = c.column_id
                        WHERE c.object_id = t.object_id
                    )
                  + CHAR(13) + CHAR(10) + N'');''
            FROM ' + QUOTENAME(@db) + N'.sys.tables AS t
            JOIN ' + QUOTENAME(@db) + N'.sys.schemas AS s
              ON s.schema_id = t.schema_id
            WHERE t.is_ms_shipped = 0
              AND s.name <> ''sys''
              AND EXISTS (SELECT 1 FROM ' + QUOTENAME(@db) + N'.sys.columns c
                          WHERE c.object_id = t.object_id);
            ';

            BEGIN TRY
                EXEC sys.sp_executesql @sql, N'@dbParam sysname', @dbParam = @db;
            END TRY
            BEGIN CATCH
                PRINT 'Tables skipped for [' + @db + ']: ' + ERROR_MESSAGE();
            END CATCH
        END
    END

    FETCH NEXT FROM curDBs INTO @db;
END

CLOSE curDBs;
DEALLOCATE curDBs;

--------------------------------------------------------------------------------
-- FLATTEN + CSV
--   Newlines -> _LINEBREAK_ so one object is one line. CRLF first, or it
--   would leave a stray marker per line. Every field is quoted and inner
--   quotes doubled, which Python's csv.reader unwinds exactly.
--------------------------------------------------------------------------------
IF OBJECT_ID('tempdb..#Flat') IS NOT NULL DROP TABLE #Flat;

SELECT
    e.DatabaseName,
    e.ObjectType,
    e.SchemaName,
    e.ObjectName,
    e.ObjectTimestamp,
    Definition = REPLACE(REPLACE(REPLACE(
                     ISNULL(e.Definition, N''),
                     CHAR(13) + CHAR(10), N'_LINEBREAK_'),
                     CHAR(10),            N'_LINEBREAK_'),
                     CHAR(13),            N'_LINEBREAK_'),
    Extra = CAST(NULL AS nvarchar(1))
INTO #Flat
FROM #ObjectExport AS e;

IF OBJECT_ID('tempdb..#Csv') IS NOT NULL DROP TABLE #Csv;

SELECT
    f.DatabaseName, f.ObjectType, f.SchemaName, f.ObjectName,
    f.ObjectTimestamp, f.Definition, f.Extra,
    DefinitionLength = LEN(f.Definition),
    CsvLine =
          N'"' + REPLACE(f.DatabaseName, N'"', N'""') + N'",'
        + N'"' + f.ObjectType + N'",'
        + N'"' + REPLACE(f.SchemaName,   N'"', N'""') + N'",'
        + N'"' + REPLACE(f.ObjectName,   N'"', N'""') + N'",'
        + N'"' + ISNULL(CAST(f.ObjectTimestamp AS nvarchar(20)), N'') + N'",'
        + N'"' + REPLACE(f.Definition,   N'"', N'""') + N'",'
INTO #Csv
FROM #Flat AS f;

--------------------------------------------------------------------------------
-- OPTIONAL: leave a permanent table for bcp (which connects as its own
-- session and therefore cannot see a temp table of any kind).
--------------------------------------------------------------------------------
IF @PersistTable = 1
BEGIN
    BEGIN TRY
        SET @sql = N'IF OBJECT_ID(''' + @PersistTableName + N''') IS NOT NULL DROP TABLE ' + @PersistTableName + N';';
        EXEC sys.sp_executesql @sql;

        SET @sql = N'SELECT DatabaseName, ObjectType, SchemaName, ObjectName, ObjectTimestamp,
                            Definition, DefinitionLength, CsvLine
                     INTO ' + @PersistTableName + N' FROM #Csv;';
        EXEC sys.sp_executesql @sql;

        PRINT 'Persisted to ' + @PersistTableName + ' — bcp it out (see header).';
    END TRY
    BEGIN CATCH
        PRINT 'Could not persist to ' + @PersistTableName + ': ' + ERROR_MESSAGE();
        PRINT 'Set @PersistTable = 0 and use the grid path, but read the truncation warning first.';
    END CATCH
END

--------------------------------------------------------------------------------
-- RESULT SET 1 — run info. Read this before trusting the export.
--------------------------------------------------------------------------------
SELECT
    SnapshotDT             = @SnapshotDT,
    DatabasesHarvested     = (SELECT COUNT(DISTINCT DatabaseName) FROM #ObjectExport),
    DatabasesExcluded      = (SELECT COUNT(*) FROM #DbExcluded),
    Objects                = (SELECT COUNT(*) FROM #ObjectExport),
    Procedures             = (SELECT COUNT(*) FROM #ObjectExport WHERE ObjectType = 'PROC'),
    Views                  = (SELECT COUNT(*) FROM #ObjectExport WHERE ObjectType = 'VIEW'),
    Tables                 = (SELECT COUNT(*) FROM #ObjectExport WHERE ObjectType = 'TABLE'),
    Encrypted              = (SELECT COUNT(*) FROM #Unreadable WHERE Reason = 'encrypted'),
    NoDefinition           = (SELECT COUNT(*) FROM #Unreadable WHERE Reason = 'no-definition'),
    -- If this is not zero, the SSMS grid path will silently truncate. Use bcp.
    OverGridCellLimit      = (SELECT COUNT(*) FROM #Csv WHERE DefinitionLength > @GridCellLimit),
    LargestDefinitionChars = (SELECT MAX(DefinitionLength) FROM #Csv);

--------------------------------------------------------------------------------
-- RESULT SET 2 — the export, in the loader's column order.
--   Deterministic order so two runs diff cleanly.
--------------------------------------------------------------------------------
SELECT DatabaseName, ObjectType, SchemaName, ObjectName, ObjectTimestamp, Definition, Extra
FROM #Csv
ORDER BY DatabaseName, ObjectType, SchemaName, ObjectName;

--------------------------------------------------------------------------------
-- Uncomment as needed:
-- SELECT CsvLine FROM #Csv ORDER BY DatabaseName, ObjectType, SchemaName, ObjectName;
-- SELECT * FROM #Unreadable ORDER BY DatabaseName, ObjectType, SchemaName, ObjectName;
-- SELECT * FROM #DbExcluded;              -- confirm the pattern list caught what you meant
-- SELECT DatabaseName, ObjectType, COUNT(*) FROM #ObjectExport GROUP BY DatabaseName, ObjectType ORDER BY 1, 2;
--------------------------------------------------------------------------------
