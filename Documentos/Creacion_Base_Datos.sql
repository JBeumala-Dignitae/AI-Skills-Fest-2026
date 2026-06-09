-- =========================================================
-- WORKSHOP CREACION BBDD
-- Registro Horario Inteligente
-- Para PostgreSQL
-- =========================================================

-- =========================================================
-- 1. CREAMOS LA BASE DE DATOS
-- =========================================================

CREATE DATABASE ws_reghorario;

-- =========================================================
-- 2. CREAMOS UN USUARIO
-- =========================================================

CREATE USER ws_usuario
WITH
    PASSWORD '***********';

-- =========================================================
-- 3. Damos privilegios
-- =========================================================

GRANT ALL PRIVILEGES ON DATABASE ws_reghorario TO ws_usuario;

-- =========================================================
-- CONNECT TO DATABASE ???
-- =========================================================
-- Execute manually in psql or pgAdmin:
--
-- \c ws_reg_horario
--
-- =========================================================


-- =========================================================
-- 4. GRANT SCHEMA PRIVILEGES
-- =========================================================

GRANT ALL ON SCHEMA public TO ws_usuario;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
GRANT ALL ON TABLES TO ws_usuario;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
GRANT ALL ON SEQUENCES TO ws_usuario;

-- =========================================================
-- 5. Tablas
-- =========================================================
-- REGISTROS TIEMPO
-- Creamos secuencia
CREATE SEQUENCE ws_registros_tiempo_sec_id
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

-- Creamos tipos de dato ENUM
CREATE TYPE estado_reghor AS ENUM ('entrada pendiente', 'salida pendiente', 'completo', 'corregido');
CREATE TYPE origen_reghor AS ENUM ('api', 'webapp', 'agenteIA');

-- Creamos tabla
CREATE TABLE ws_registros_tiempo (
    id BIGINT NOT NULL PRIMARY KEY DEFAULT nextval('ws_registros_tiempo_sec_id'),
    upn_empleado VARCHAR(255) NOT NULL,
    entrada TIMESTAMPTZ NOT NULL,
    salida TIMESTAMPTZ NULL,
    segundos_trabajados INT,
    estado estado_reghor NOT NULL DEFAULT 'salida pendiente',
    origen origen_reghor NOT NULL DEFAULT 'webapp',
    comentario_justificacion TEXT,
	upn_alta VARCHAR(255) NOT NULL,
	upn_ultima_modificacion VARCHAR(255) NOT NULL,
    fecha_alta TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    fecha_ultima_modificacion TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- =========================================================
-- 6. INDICES 
-- =========================================================

CREATE INDEX ws_idx_registros_tiempo_empleado
    ON ws_registros_tiempo ( upn_empleado, entrada );

CREATE INDEX ws_idx_registros_tiempo_entrada
    ON ws_registros_tiempo ( entrada );
