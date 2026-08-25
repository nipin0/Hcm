/** Copy trading configuration type definitions. */

// ── Enum Types ──────────────────────────────────────

export type CopyStatus = 'running' | 'stopped' | 'paused';

export type LotMode = 'multiplier' | 'fixed' | 'balance_ratio';

export type DirectionMode = 'FORWARD' | 'REVERSE' | 'BOTH';

export type SyncMode = 'pubsub' | 'poll';

export type MatchMode = 'exact' | 'prefix' | 'suffix' | 'manual';

export type CopyTradeStatus = 'success' | 'failed' | 'pending' | 'timeout';

// ── Core Interfaces ─────────────────────────────────

/** A copy trading relationship between a master and follower account. */
export interface CopyRelationship {
  relationship_id: number;
  master_account_id: number;
  copy_account_id: number;
  master_account_name?: string;
  copy_account_name?: string;
  broker_name?: string;
  status: CopyStatus;
  lot_mode: LotMode;
  lot_multiplier: number;
  min_lot: number;
  max_lot: number;
  max_positions: number;
  max_daily_loss: number;
  max_daily_profit: number;
  circuit_break_enabled: boolean;
  max_consecutive_losses: number;
  direction_mode: DirectionMode;
  copy_sl: boolean;
  copy_tp: boolean;
  sync_mode: SyncMode;
  retry_on_failure: boolean;
  retry_max: number;
  max_slippage_pips?: number;
  max_execution_delay_ms?: number;
  created_at: string;
  updated_at: string;
}

/** Request body for creating a copy relationship. */
export interface CopyRelationshipCreate {
  master_account_id: number;
  copy_account_id: number;
  status?: CopyStatus;
  lot_mode?: LotMode;
  lot_multiplier?: number;
  min_lot?: number;
  max_lot?: number;
  max_positions?: number;
  max_daily_loss?: number;
  max_daily_profit?: number;
  circuit_break_enabled?: boolean;
  max_consecutive_losses?: number;
  direction_mode?: DirectionMode;
  copy_sl?: boolean;
  copy_tp?: boolean;
  sync_mode?: SyncMode;
  retry_on_failure?: boolean;
  retry_max?: number;
  max_slippage_pips?: number;
  max_execution_delay_ms?: number;
}

/** Request body for updating a copy relationship. */
export interface CopyRelationshipUpdate {
  status?: CopyStatus;
  lot_mode?: LotMode;
  lot_multiplier?: number;
  min_lot?: number;
  max_lot?: number;
  max_positions?: number;
  max_daily_loss?: number;
  max_daily_profit?: number;
  circuit_break_enabled?: boolean;
  max_consecutive_losses?: number;
  direction_mode?: DirectionMode;
  copy_sl?: boolean;
  copy_tp?: boolean;
  sync_mode?: SyncMode;
  retry_on_failure?: boolean;
  retry_max?: number;
  max_slippage_pips?: number;
  max_execution_delay_ms?: number;
}

/** A cross-broker symbol mapping between master and follower instruments. */
export interface SymbolMapping {
  mapping_id: number;
  master_broker: string;
  master_symbol: string;
  follower_broker: string;
  follower_symbol: string;
  match_mode: MatchMode;
  match_priority: number;
  is_active: boolean;
  created_at?: string;
  updated_at?: string;
}

/** Request body for creating a symbol mapping. */
export interface SymbolMappingCreate {
  master_broker: string;
  master_symbol: string;
  follower_broker?: string;
  follower_symbol: string;
  match_mode?: MatchMode;
  match_priority?: number;
  is_active?: boolean;
}

/** Request body for updating a symbol mapping. */
export interface SymbolMappingUpdate {
  master_broker?: string;
  follower_broker?: string;
  follower_symbol?: string;
  match_mode?: MatchMode;
  match_priority?: number;
}

/** A trade execution log entry for a copy relationship. */
export interface CopyTradeLog {
  log_id: number;
  relationship_id: number;
  signal_id?: string;
  trade_id?: string;
  symbol: string;
  direction: string;
  lot: number;
  entry_price: number;
  exit_price?: number;
  pnl?: number;
  profit?: number;
  status: CopyTradeStatus;
  latency_ms?: number;
  error_message?: string;
  message?: string;
  created_at: string;
}

/** A broker/exchange account available for copy trading. */
export interface CopyAccount {
  account_id: number;
  account_name: string;
  account_number: string;
  broker_name: string;
  server_name: string;
  account_type: string;
}

/** A distinct broker name. */
export interface CopyBroker {
  broker_name: string;
}

/** Paginated API response wrapper. */
export interface PaginatedResponse<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}

/** Standard API response envelope. */
export interface ApiResponse<T> {
  code: number;
  data: T;
  message: string;
}
