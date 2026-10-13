-- SQL schema for Supabase / PostgreSQL cloud mode

create table if not exists runs (
  run_id text primary key,
  status text not null,
  request_text text,
  understood_goal jsonb,
  plan jsonb,
  artifacts jsonb,
  failure_reason text,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

create table if not exists events (
  run_id text not null,
  seq integer not null,
  ts timestamptz not null default now(),
  type text not null,
  payload jsonb default '{}'::jsonb,
  primary key (run_id, seq)
);

create table if not exists artifacts (
  id text primary key,
  run_id text not null,
  kind text not null,
  bucket text not null,
  path text not null,
  filename text not null,
  sha256 text not null,
  size_bytes bigint not null,
  created_at timestamptz default now()
);

create index if not exists idx_artifacts_run_id on artifacts(run_id);
create index if not exists idx_artifacts_path on artifacts(path);

create table if not exists feedback_rules (
  id text primary key,
  question text not null,
  answer text not null,
  approver text not null,
  notes text,
  created_at timestamptz default now()
);

create table if not exists kb_documents (
  doc_id text primary key,
  doc_name text not null,
  version text not null,
  effective_date text not null,
  source_hash text not null,
  file_type text not null,
  chunk_count integer not null default 0,
  bucket text,
  path text,
  created_at timestamptz default now()
);

create table if not exists kb_chunks (
  chunk_id text primary key,
  doc_id text not null references kb_documents(doc_id) on delete cascade,
  doc_name text,
  version text,
  effective_date text,
  content text not null,
  weight double precision default 1.0,
  source_hash text,
  created_at timestamptz default now()
);

create index if not exists idx_kb_chunks_doc_id on kb_chunks(doc_id);

