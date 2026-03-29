-- Private Financial AI - Database Schema
-- Run this to initialize an empty database:
-- sqlite3 ~/private-financial-ai/vault/databases/main.db < database/schema.sql

-- =============================================================================
-- BANK ACCOUNTS & TRANSACTIONS
-- =============================================================================

-- Bank accounts (checking, savings, credit cards)
CREATE TABLE IF NOT EXISTS accounts (
    account_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT,                          -- checking, savings, credit, etc.
    institution TEXT,
    mask TEXT,                          -- Last 4 digits
    current_balance REAL,
    available_balance REAL,
    credit_limit REAL,                  -- For credit cards
    is_active INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Transactions from all sources
CREATE TABLE IF NOT EXISTS transactions (
    transaction_id TEXT PRIMARY KEY,
    account_id TEXT,
    date DATE NOT NULL,
    amount REAL NOT NULL,               -- Negative = expense, Positive = income
    description TEXT,
    merchant_name TEXT,
    category TEXT,                      -- Original category from source
    category_normalized TEXT,           -- Standardized category

    -- Source tracking
    source_type TEXT DEFAULT 'csv',     -- 'csv', 'plaid'
    plaid_transaction_id TEXT,

    -- Deduplication
    is_duplicate INTEGER DEFAULT 0,
    duplicate_of_txn_id TEXT,
    is_transfer INTEGER DEFAULT 0,      -- Internal transfers (not expenses)
    transfer_pair_id TEXT,

    -- Metadata
    pending INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (account_id) REFERENCES accounts(account_id)
);

CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions(date);
CREATE INDEX IF NOT EXISTS idx_transactions_category ON transactions(category_normalized);
CREATE INDEX IF NOT EXISTS idx_transactions_account ON transactions(account_id);

-- =============================================================================
-- PLAID INTEGRATION
-- =============================================================================

-- Plaid items (bank connections)
CREATE TABLE IF NOT EXISTS plaid_items (
    item_id TEXT PRIMARY KEY,
    access_token TEXT NOT NULL,
    institution_id TEXT,
    institution_name TEXT,
    status TEXT DEFAULT 'active',       -- active, error, pending_expiration
    error_code TEXT,
    error_message TEXT,
    consent_expiration_time TIMESTAMP,
    sync_cursor TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Plaid accounts linked to items
CREATE TABLE IF NOT EXISTS plaid_accounts (
    plaid_account_id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL,
    account_id TEXT,                    -- Links to accounts table
    name TEXT,
    official_name TEXT,
    type TEXT,
    subtype TEXT,
    mask TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (item_id) REFERENCES plaid_items(item_id),
    FOREIGN KEY (account_id) REFERENCES accounts(account_id)
);

-- =============================================================================
-- INVESTMENTS
-- =============================================================================

-- Investment accounts (401k, IRA, brokerage, etc.)
CREATE TABLE IF NOT EXISTS investment_accounts (
    account_id TEXT PRIMARY KEY,
    account_name TEXT NOT NULL,
    institution TEXT,
    account_type TEXT,                  -- 401k, IRA, Roth IRA, Brokerage, HSA
    is_active INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Holdings in investment accounts
CREATE TABLE IF NOT EXISTS holdings (
    holding_id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    symbol TEXT,
    name TEXT,
    quantity REAL,
    price REAL,
    current_value REAL,
    cost_basis REAL,
    asset_type TEXT,                    -- Stock, ETF, Mutual Fund, Bond, Cash
    is_active INTEGER DEFAULT 1,
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (account_id) REFERENCES investment_accounts(account_id)
);

CREATE INDEX IF NOT EXISTS idx_holdings_account ON holdings(account_id);

-- =============================================================================
-- CRYPTOCURRENCY
-- =============================================================================

-- Bitcoin wallets (tracked via xpub)
CREATE TABLE IF NOT EXISTS bitcoin_wallets (
    wallet_id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL,
    xpub TEXT,                          -- Extended public key
    address TEXT,                       -- Or single address
    balance_btc REAL,
    balance_usd REAL,
    last_updated TIMESTAMP
);

-- EVM wallet addresses
CREATE TABLE IF NOT EXISTS crypto_wallets (
    wallet_id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL,
    address TEXT NOT NULL,              -- 0x... address
    chain TEXT DEFAULT 'ethereum',      -- ethereum, polygon, arbitrum, etc.
    is_active INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Token balances
CREATE TABLE IF NOT EXISTS crypto_balances (
    balance_id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_id INTEGER,
    chain TEXT,
    token_symbol TEXT,
    token_name TEXT,
    token_address TEXT,
    balance REAL,
    balance_usd REAL,
    price_usd REAL,
    last_updated TIMESTAMP,

    FOREIGN KEY (wallet_id) REFERENCES crypto_wallets(wallet_id)
);

-- DeFi positions (Aave, Uniswap, etc.)
CREATE TABLE IF NOT EXISTS defi_positions (
    position_id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_id INTEGER,
    protocol TEXT,                      -- Aave, Uniswap, Compound, etc.
    chain TEXT,
    position_type TEXT,                 -- lending, borrowing, liquidity, staking
    balance_usd REAL,
    last_updated TIMESTAMP,

    FOREIGN KEY (wallet_id) REFERENCES crypto_wallets(wallet_id)
);

-- DeFi position details (collateral, debt, rewards)
CREATE TABLE IF NOT EXISTS defi_position_details (
    detail_id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER,
    detail_type TEXT,                   -- APP_TOKEN (supplied), BORROWED, CLAIMABLE
    token_symbol TEXT,
    token_name TEXT,
    balance REAL,
    balance_usd REAL,

    FOREIGN KEY (position_id) REFERENCES defi_positions(position_id)
);

-- =============================================================================
-- BUDGETS
-- =============================================================================

CREATE TABLE IF NOT EXISTS budgets (
    budget_id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL UNIQUE,      -- Matches category_normalized
    monthly_limit REAL NOT NULL,
    is_active INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- =============================================================================
-- MEMORY SYSTEM (Knowledge Graph)
-- =============================================================================

-- Entities (people, goals, accounts, etc.)
CREATE TABLE IF NOT EXISTS entities (
    entity_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    entity_type TEXT,                   -- person, goal, employer, account, etc.
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Observations about entities
CREATE TABLE IF NOT EXISTS observations (
    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    source TEXT,                        -- Where this info came from
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (entity_id) REFERENCES entities(entity_id) ON DELETE CASCADE
);

-- Relations between entities
CREATE TABLE IF NOT EXISTS relations (
    relation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_entity_id INTEGER NOT NULL,
    to_entity_id INTEGER NOT NULL,
    relation_type TEXT NOT NULL,        -- spouse_of, works_at, has_goal, etc.
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (from_entity_id) REFERENCES entities(entity_id) ON DELETE CASCADE,
    FOREIGN KEY (to_entity_id) REFERENCES entities(entity_id) ON DELETE CASCADE
);

-- =============================================================================
-- INVESTMENT BELIEFS (with embeddings for semantic search)
-- =============================================================================

CREATE TABLE IF NOT EXISTS investment_beliefs (
    belief_id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,             -- general_philosophy, risk_tolerance, asset_allocation, tax, crypto
    belief TEXT NOT NULL,
    source TEXT,
    confidence REAL DEFAULT 1.0,
    embedding BLOB,                     -- For semantic search
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- =============================================================================
-- DOCUMENT VAULT
-- =============================================================================

CREATE TABLE IF NOT EXISTS vault_documents (
    document_id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    original_filename TEXT,
    file_path TEXT NOT NULL,
    file_size INTEGER,
    mime_type TEXT,

    -- Document metadata
    document_type TEXT,                 -- insurance, will, trust, contract, benefits
    provider TEXT,                      -- Insurance company, etc.
    policy_number TEXT,

    -- Extracted content
    extracted_text TEXT,
    summary TEXT,

    -- Important dates
    effective_date DATE,
    expiration_date DATE,

    -- Organization
    tags TEXT,                          -- JSON array of tags

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_vault_type ON vault_documents(document_type);
CREATE INDEX IF NOT EXISTS idx_vault_expiration ON vault_documents(expiration_date);

-- =============================================================================
-- CHAT HISTORY
-- =============================================================================

CREATE TABLE IF NOT EXISTS conversations (
    conversation_id TEXT PRIMARY KEY,
    title TEXT,
    project_id INTEGER,
    model TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS conversation_messages (
    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    role TEXT NOT NULL,                 -- user, assistant, system
    content TEXT NOT NULL,
    model TEXT,
    tokens_in INTEGER,
    tokens_out INTEGER,
    cost REAL,
    tools_used TEXT,                    -- JSON array of tool names
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation ON conversation_messages(conversation_id);

-- Session attachments (files uploaded to conversations)
CREATE TABLE IF NOT EXISTS session_attachments (
    attachment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    original_filename TEXT,
    file_path TEXT NOT NULL,
    file_size INTEGER,
    mime_type TEXT,
    extracted_text TEXT,
    base64_content TEXT,                -- For images
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id) ON DELETE CASCADE
);

-- =============================================================================
-- RESEARCH PROJECTS
-- =============================================================================

CREATE TABLE IF NOT EXISTS projects (
    project_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT,
    topic TEXT,                         -- college, retirement, tax, real_estate, etc.
    status TEXT DEFAULT 'active',       -- active, completed, archived
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS project_findings (
    finding_id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    finding_type TEXT,                  -- insight, recommendation, question, resource
    content TEXT NOT NULL,
    source TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS project_messages (
    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE CASCADE
);

-- =============================================================================
-- SYSTEM TABLES
-- =============================================================================

-- API usage tracking
CREATE TABLE IF NOT EXISTS api_usage (
    usage_id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,             -- anthropic, openai, claude_cli
    model TEXT,
    tokens_in INTEGER,
    tokens_out INTEGER,
    cost REAL,
    query_type TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_api_usage_date ON api_usage(created_at);

-- Response feedback for quality tracking
CREATE TABLE IF NOT EXISTS response_feedback (
    feedback_id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT,
    message_id INTEGER,
    model TEXT,
    rating INTEGER,                     -- 1 = thumbs up, -1 = thumbs down
    tools_used TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Category rules for transaction categorization
CREATE TABLE IF NOT EXISTS category_rules (
    rule_id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern TEXT NOT NULL,              -- Merchant/description pattern
    category TEXT NOT NULL,             -- Normalized category
    priority INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- File registry (tracks imported files)
CREATE TABLE IF NOT EXISTS file_registry (
    file_id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    file_hash TEXT,
    file_type TEXT,                     -- transactions, portfolio, etc.
    records_imported INTEGER,
    imported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
