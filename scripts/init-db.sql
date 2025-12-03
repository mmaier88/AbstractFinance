-- AbstractFinance Database Schema

-- Portfolio snapshots table
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id SERIAL PRIMARY KEY,
    timestamp TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    nav DECIMAL(18, 2),
    cash DECIMAL(18, 2),
    gross_exposure DECIMAL(18, 2),
    net_exposure DECIMAL(18, 2),
    realized_vol DECIMAL(8, 4),
    max_drawdown DECIMAL(8, 4),
    current_drawdown DECIMAL(8, 4),
    daily_pnl DECIMAL(18, 2),
    daily_return DECIMAL(8, 6),
    hedge_budget_used DECIMAL(18, 2)
);

-- Positions table
CREATE TABLE IF NOT EXISTS positions (
    id SERIAL PRIMARY KEY,
    timestamp TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    instrument_id VARCHAR(50),
    quantity DECIMAL(18, 6),
    avg_cost DECIMAL(18, 6),
    market_price DECIMAL(18, 6),
    market_value DECIMAL(18, 2),
    unrealized_pnl DECIMAL(18, 2),
    sleeve VARCHAR(50),
    UNIQUE(timestamp, instrument_id)
);

-- Orders table
CREATE TABLE IF NOT EXISTS orders (
    id SERIAL PRIMARY KEY,
    order_id VARCHAR(100) UNIQUE,
    timestamp TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    instrument_id VARCHAR(50),
    side VARCHAR(10),
    quantity DECIMAL(18, 6),
    order_type VARCHAR(20),
    limit_price DECIMAL(18, 6),
    status VARCHAR(20),
    filled_qty DECIMAL(18, 6),
    avg_fill_price DECIMAL(18, 6),
    commission DECIMAL(18, 6),
    sleeve VARCHAR(50),
    reason TEXT
);

-- Risk decisions table
CREATE TABLE IF NOT EXISTS risk_decisions (
    id SERIAL PRIMARY KEY,
    timestamp TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    scaling_factor DECIMAL(8, 4),
    realized_vol DECIMAL(8, 4),
    target_vol DECIMAL(8, 4),
    current_drawdown DECIMAL(8, 4),
    regime VARCHAR(20),
    emergency_derisk BOOLEAN,
    warnings TEXT[]
);

-- Hedge positions table
CREATE TABLE IF NOT EXISTS hedge_positions (
    id SERIAL PRIMARY KEY,
    hedge_id VARCHAR(100) UNIQUE,
    timestamp TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    hedge_type VARCHAR(50),
    instrument_id VARCHAR(50),
    underlying VARCHAR(50),
    quantity INTEGER,
    strike DECIMAL(18, 6),
    expiry DATE,
    premium_paid DECIMAL(18, 2),
    current_value DECIMAL(18, 2),
    is_active BOOLEAN DEFAULT TRUE
);

-- Daily returns table
CREATE TABLE IF NOT EXISTS daily_returns (
    id SERIAL PRIMARY KEY,
    date DATE UNIQUE,
    daily_return DECIMAL(10, 8),
    nav DECIMAL(18, 2),
    cumulative_return DECIMAL(10, 8)
);

-- Alerts table
CREATE TABLE IF NOT EXISTS alerts (
    id SERIAL PRIMARY KEY,
    timestamp TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    alert_type VARCHAR(50),
    severity VARCHAR(20),
    title VARCHAR(200),
    message TEXT,
    acknowledged BOOLEAN DEFAULT FALSE
);

-- Create indexes
CREATE INDEX IF NOT EXISTS idx_portfolio_snapshots_timestamp ON portfolio_snapshots(timestamp);
CREATE INDEX IF NOT EXISTS idx_positions_timestamp ON positions(timestamp);
CREATE INDEX IF NOT EXISTS idx_positions_instrument ON positions(instrument_id);
CREATE INDEX IF NOT EXISTS idx_orders_timestamp ON orders(timestamp);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_risk_decisions_timestamp ON risk_decisions(timestamp);
CREATE INDEX IF NOT EXISTS idx_daily_returns_date ON daily_returns(date);
CREATE INDEX IF NOT EXISTS idx_alerts_timestamp ON alerts(timestamp);
CREATE INDEX IF NOT EXISTS idx_alerts_severity ON alerts(severity);

-- Grant permissions
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO postgres;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO postgres;
