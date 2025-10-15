-- Farm Monitoring Database Schema
-- Usage: psql "$DATABASE_URL" -f schema.sql
-- The database supports a few different types of sensors in a standardized format.
-- NOTE: The image_asset table and its index are commented out for now.

\set ON_ERROR_STOP on
set client_min_messages to warning;
set timezone to 'UTC';

create extension if not exists pgcrypto; -- for gen_random_uuid()

create table if not exists sensor (
  hardware_id   bigint primary key,
  name          text not null,
  sensor_type   text not null,
  gps_latitude  double precision,
  gps_longitude double precision,
  metadata      jsonb not null default '{}'::jsonb
);

create table if not exists reading (
  id           uuid primary key default gen_random_uuid(),
  sensor_id    bigint not null references sensor(hardware_id) on delete cascade,
  ts           timestamptz not null,
  sequence     integer,
  temperature_c double precision,
  humidity_pct  double precision,
  capacitance_val double precision,
  battery_v     double precision,
  rssi_dbm      smallint,
  unique(sensor_id, ts)
);

/*
-- Image assets are not used currently; enable when needed
create table if not exists image_asset (
  id           uuid primary key default gen_random_uuid(),
  reading_id   uuid not null references reading(id) on delete cascade,
  sensor_id    bigint not null references sensor(hardware_id) on delete cascade,
  ts           timestamptz not null,
  s3_bucket    text not null,
  s3_key       text not null,
  content_type text not null default 'image/jpeg',
  bytes        bigint not null,
  width        integer,
  height       integer,
  etag         text,
  sha256       bytea,
  exif         jsonb not null default '{}'::jsonb,
  unique(s3_bucket, s3_key)
);
*/

create index if not exists idx_reading_sensor_ts_desc on reading(sensor_id, ts desc);

/* create index if not exists idx_image_asset_sensor_ts_desc on image_asset(sensor_id, ts desc); */


