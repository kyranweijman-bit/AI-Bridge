-- AI Bridge database schema (PostgreSQL).
--
-- db.py runs this automatically when the app starts, and every statement is
-- "if not exists", so running it again is harmless. You can also paste it
-- into Supabase's SQL Editor yourself.

-- One row per chat in the sidebar.
create table if not exists chats (
    id          uuid primary key default gen_random_uuid(),
    title       text not null default 'New chat',
    pinned      boolean not null default false,
    owner       text,                     -- reserved for per-person accounts later
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

-- One row per piece of conversation: your question, each model's answer,
-- a review, a comparison, or one debate reply. A "turn" is one press of Ask;
-- every row from that press shares the same turn_index.
create table if not exists messages (
    id          bigserial primary key,
    chat_id     uuid not null references chats(id) on delete cascade,
    turn_index  int not null,
    mode        text not null,            -- single | independent | debate | solo
    kind        text not null,            -- question | answer | review | comparison | debate
    model       text,                     -- Claude | ChatGPT (null for your question)
    role        text,                     -- debate role, e.g. Proposer / Critic
    round       int,                      -- debate round number
    content     text not null,            -- what's shown on screen
    context     text,                     -- what the models were sent, if different (e.g. question + file text)
    in_context  boolean not null default false,  -- included in follow-up questions?
    meta        jsonb not null default '{}',
    created_at  timestamptz not null default now()
);
create index if not exists messages_chat_idx on messages (chat_id, turn_index, id);

-- Things both models should know. chat_id null = remembered in every chat.
create table if not exists memories (
    id          bigserial primary key,
    chat_id     uuid references chats(id) on delete cascade,
    content     text not null,
    created_at  timestamptz not null default now()
);

-- One row per API call, so costs survive restarts and can be split per
-- chat, per day or per model.
create table if not exists usage_log (
    id             bigserial primary key,
    chat_id        uuid references chats(id) on delete set null,
    provider       text not null,         -- anthropic | openai
    model          text not null,
    input_tokens   int not null,
    output_tokens  int not null,
    cost           numeric,               -- estimate from PRICING in bridge.py; null if unknown model
    created_at     timestamptz not null default now()
);
create index if not exists usage_log_created_idx on usage_log (created_at);

-- Attached files. The extracted text is always stored here; the original
-- file is also kept in Supabase Storage if that's configured.
create table if not exists files (
    id              bigserial primary key,
    chat_id         uuid references chats(id) on delete cascade,
    turn_index      int,
    filename        text not null,
    storage_path    text,                 -- null if the original wasn't uploaded
    extracted_text  text,
    created_at      timestamptz not null default now()
);

-- "Edit my file" (Built 48) needs the original bytes back later, regardless
-- of whether Supabase Storage is configured, to carry over a .docx's styles
-- or a .pdf's page size when building the edited download - so those get a
-- copy kept right here too, but only for files attached with that feature
-- turned on (see db.py's save_file(keep_original=...)), not every attachment.
alter table files add column if not exists original_bytes bytea;

-- Small app-wide settings, one row per key - just the spending limit for
-- now. A key-value table instead of a dedicated column since this is meant
-- to grow (more app-wide toggles later) without another migration each time.
create table if not exists settings (
    key         text primary key,
    value       text not null,
    updated_at  timestamptz not null default now()
);

-- Blind-judging votes (quick win): which answer someone preferred before
-- the model names were revealed, for Independent Answers turns asked with
-- that toggle on. Also kept around for the model-vs-model stats dashboard
-- further down the roadmap, once that gets built.
create table if not exists votes (
    id          bigserial primary key,
    chat_id     uuid not null references chats(id) on delete cascade,
    turn_index  int not null,
    winner      text not null,          -- Claude | ChatGPT | Tie
    created_at  timestamptz not null default now()
);

-- Saveable personas (more roles and personas quick idea): a name plus a
-- free-text instruction, assignable to either model's debate role
-- alongside the fixed Proposer/Critic/Fact-checker/Devil's advocate ones.
create table if not exists personas (
    id           bigserial primary key,
    name         text not null unique,
    instruction  text not null,
    created_at   timestamptz not null default now()
);

-- Editable working brief: one per chat - a standing summary of the goal,
-- constraints, decisions and open questions, sent to both models on every
-- call in that chat (like memory, but a single editable block rather than
-- a growing list of notes).
create table if not exists briefs (
    chat_id     uuid primary key references chats(id) on delete cascade,
    content     text not null,
    updated_at  timestamptz not null default now()
);

-- Decision journal: what you chose, why, and (filled in later) what
-- actually happened - so real outcomes can eventually be compared against
-- the advice that led to them.
create table if not exists decisions (
    id          bigserial primary key,
    chat_id     uuid not null references chats(id) on delete cascade,
    turn_index  int not null,
    choice      text not null,
    reasoning   text,
    outcome     text,                 -- filled in later, once you know what happened
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

-- Supabase also exposes every table through a public web API. Turning on
-- row-level security with no policies blocks that route completely, while
-- the app (which connects directly as the database owner) is unaffected.
alter table chats     enable row level security;
alter table messages  enable row level security;
alter table memories  enable row level security;
alter table usage_log enable row level security;
alter table files     enable row level security;
alter table settings  enable row level security;
alter table votes     enable row level security;
alter table personas  enable row level security;
alter table briefs    enable row level security;
alter table decisions enable row level security;
