-- Paste into Supabase: SQL Editor -> New query -> Run
create table if not exists opportunities (
  id text primary key,
  source text, title text, organization text, url text,
  description text, location text, posted_at text,
  tags jsonb default '[]',
  category text, badge text, blockers jsonb default '[]',
  seniority text, skill_matches jsonb default '[]', score int default 0,
  why text, country text, deadline date, paid text, visa_sponsorship text,
  timezone_note text, classified_by text,
  status text default 'new', notes text default '',
  found_at timestamptz default now()
);
create index if not exists opportunities_status_idx on opportunities (status);
create index if not exists opportunities_category_idx on opportunities (category);

-- Lock the table: only the secret service_role key (used by the pipeline and
-- the dashboard server) can read or write. The public anon key gets nothing.
alter table opportunities enable row level security;
drop policy if exists "Authenticated users can read opportunities" on opportunities;
create policy "Authenticated users can read opportunities"
  on opportunities for select to authenticated using (true);

-- One row per daily run: per-source counts, so a silently broken source is visible.
create table if not exists runs (
  id bigserial primary key,
  ran_at timestamptz default now(),
  stats jsonb
);
alter table runs enable row level security;
drop policy if exists "Authenticated users can read runs" on runs;
create policy "Authenticated users can read runs"
  on runs for select to authenticated using (true);

-- One profile per authenticated user.  The anon key can only access the
-- currently signed-in user's row; the service role remains available to
-- trusted server-side jobs.
create table if not exists profiles (
  user_id uuid primary key references auth.users(id) on delete cascade,
  full_name text default '',
  resume_text text default '',
  profile jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default now()
);
alter table profiles enable row level security;

drop policy if exists "Users can read their own profile" on profiles;
create policy "Users can read their own profile"
  on profiles for select using (auth.uid() = user_id);
drop policy if exists "Users can insert their own profile" on profiles;
create policy "Users can insert their own profile"
  on profiles for insert with check (auth.uid() = user_id);
drop policy if exists "Users can update their own profile" on profiles;
create policy "Users can update their own profile"
  on profiles for update using (auth.uid() = user_id)
  with check (auth.uid() = user_id);
drop policy if exists "Users can delete their own profile" on profiles;
create policy "Users can delete their own profile"
  on profiles for delete using (auth.uid() = user_id);

-- Per-user workflow state keeps the shared opportunity catalog separate from
-- private saves, notes, and application progress.
create table if not exists user_opportunity_state (
  user_id uuid references auth.users(id) on delete cascade,
  opportunity_id text references opportunities(id) on delete cascade,
  status text not null default 'new',
  notes text not null default '',
  updated_at timestamptz not null default now(),
  primary key (user_id, opportunity_id)
);
alter table user_opportunity_state enable row level security;
drop policy if exists "Users can manage their opportunity state" on user_opportunity_state;
create policy "Users can manage their opportunity state"
  on user_opportunity_state for all
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);
