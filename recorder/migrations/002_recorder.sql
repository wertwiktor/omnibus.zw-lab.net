-- ZW-Omnibus recorder module schema (002)
-- recording rows are written by the recorder service (service_role);
-- the SPA reads them via PostgREST under RLS (all authenticated users can
-- read everything — it's a shared knowledge base).

create table if not exists zw_omnibus.recording (
  id text primary key,                     -- folder stamp, e.g. 20260708-153000
  session_id text,
  owner_id uuid,
  owner_email text,
  title text,
  join_url text,
  status text not null default 'recording', -- recording|inbox|assigned|local_only|failed
  state text,
  share_path text,                          -- relative to share root
  local_path text,                          -- only while not yet moved / move failed
  project_dir text,                         -- share project folder name when assigned
  assigned_by text,
  assigned_at timestamptz,
  started_at timestamptz,
  ended_at timestamptz,
  duration_seconds integer,
  participants jsonb not null default '[]',
  used_identity boolean not null default false,
  error text,
  summary text not null default '',
  auto_summary text not null default '',
  auto_summary_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_recording_status on zw_omnibus.recording(status);
create index if not exists idx_recording_started on zw_omnibus.recording(started_at desc);

create or replace function zw_omnibus.touch_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end $$;

drop trigger if exists recording_touch on zw_omnibus.recording;
create trigger recording_touch before update on zw_omnibus.recording
  for each row execute function zw_omnibus.touch_updated_at();

alter table zw_omnibus.recording enable row level security;
drop policy if exists recording_read_all on zw_omnibus.recording;
create policy recording_read_all on zw_omnibus.recording
  for select to authenticated using (true);
-- authenticated users may update summary/notes fields via PostgREST; writes of
-- rows themselves come from service_role (bypasses RLS).
drop policy if exists recording_update_all on zw_omnibus.recording;
create policy recording_update_all on zw_omnibus.recording
  for update to authenticated using (true) with check (true);
grant select, update on zw_omnibus.recording to authenticated;
grant all on zw_omnibus.recording to service_role;

-- ICS calendar subscriptions (auto-join)
create table if not exists zw_omnibus.ics_calendar (
  id bigint generated always as identity primary key,
  name text not null,
  url text not null unique,
  enabled boolean not null default true,
  created_by text,
  last_polled_at timestamptz,
  last_error text,
  created_at timestamptz not null default now()
);
alter table zw_omnibus.ics_calendar enable row level security;
drop policy if exists ics_all_authenticated on zw_omnibus.ics_calendar;
create policy ics_all_authenticated on zw_omnibus.ics_calendar
  for all to authenticated using (true) with check (true);
grant all on zw_omnibus.ics_calendar to authenticated, service_role;
