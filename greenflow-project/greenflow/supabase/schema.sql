-- Run this once in your Supabase project: Project -> SQL Editor -> New query -> paste -> Run.

create table if not exists greenflow_logs (
  id              bigint generated always as identity primary key,
  created_at      timestamptz not null default now(),
  device_id       text not null,
  location        text not null,
  vehicle_count   integer not null,
  counts_by_class jsonb,
  light_status    text,          -- HIJAU / KUNING / MERAH (phase active when this row was captured)
  green_duration  integer,       -- seconds
  battery_level   integer,       -- percent, 0-100
  traffic_status  text           -- LANCAR / PADAT / MACET
);

create index if not exists greenflow_logs_created_at_idx on greenflow_logs (created_at desc);
create index if not exists greenflow_logs_location_idx on greenflow_logs (location);

-- Row Level Security: allow the dashboard (anon key, read-only) to SELECT,
-- while writes only ever come from the VPS server using the service_role
-- key (which bypasses RLS entirely).
alter table greenflow_logs enable row level security;

create policy "public read access"
  on greenflow_logs for select
  to anon
  using (true);
