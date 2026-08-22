/** Copy trading configuration API layer.
 *
 * All endpoints target /api/v1/copy/* and use the shared axios client
 * (baseURL='', JWT interceptor, 401 refresh handling).
 */

import client from './client';
import type {
  ApiResponse,
  PaginatedResponse,
  CopyRelationship,
  CopyRelationshipCreate,
  CopyRelationshipUpdate,
  SymbolMapping,
  SymbolMappingCreate,
  SymbolMappingUpdate,
  CopyTradeLog,
  CopyAccount,
  CopyBroker,
  CopyStatus,
} from '../types/copy';

// ── Relationship Filters ────────────────────────────

export interface RelationshipFilters {
  status?: CopyStatus;
  master_id?: number;
  page?: number;
  page_size?: number;
}

// ── Relationships CRUD ──────────────────────────────

/** Fetch paginated list of copy relationships with optional filters. */
export async function fetchRelationships(
  filters: RelationshipFilters = {},
): Promise<ApiResponse<PaginatedResponse<CopyRelationship>>> {
  const params: Record<string, string | number> = {};
  if (filters.status) params.status = filters.status;
  if (filters.master_id !== undefined) params.master_id = filters.master_id;
  if (filters.page !== undefined) params.page = filters.page;
  if (filters.page_size !== undefined) params.page_size = filters.page_size;

  const { data } = await client.get('/api/v1/copy/relationships', { params });
  return data;
}

/** Create a new copy trading relationship. */
export async function createRelationship(
  payload: CopyRelationshipCreate,
): Promise<ApiResponse<CopyRelationship>> {
  const { data } = await client.post('/api/v1/copy/relationships', payload);
  return data;
}

/** Update an existing copy relationship (partial update). */
export async function updateRelationship(
  id: number,
  payload: CopyRelationshipUpdate,
): Promise<ApiResponse<CopyRelationship>> {
  const { data } = await client.put(`/api/v1/copy/relationships/${id}`, payload);
  return data;
}

/** Delete a copy relationship by ID. */
export async function deleteRelationship(
  id: number,
): Promise<ApiResponse<{ relationship_id: number; deleted: boolean }>> {
  const { data } = await client.delete(`/api/v1/copy/relationships/${id}`);
  return data;
}

// ── Symbol Mappings CRUD ────────────────────────────

/** Fetch paginated list of symbol mappings. */
export async function fetchSymbolMappings(
  page: number = 1,
  pageSize: number = 50,
): Promise<ApiResponse<PaginatedResponse<SymbolMapping>>> {
  const { data } = await client.get('/api/v1/copy/symbol-mappings', {
    params: { page, page_size: pageSize },
  });
  return data;
}

/** Create a new symbol mapping. */
export async function createSymbolMapping(
  payload: SymbolMappingCreate,
): Promise<ApiResponse<SymbolMapping>> {
  const { data } = await client.post('/api/v1/copy/symbol-mappings', payload);
  return data;
}

/** Update an existing symbol mapping (partial update). */
export async function updateSymbolMapping(
  id: number,
  payload: SymbolMappingUpdate,
): Promise<ApiResponse<SymbolMapping>> {
  const { data } = await client.put(`/api/v1/copy/symbol-mappings/${id}`, payload);
  return data;
}

/** Delete a symbol mapping by ID. */
export async function deleteSymbolMapping(
  id: number,
): Promise<ApiResponse<{ mapping_id: number; deleted: boolean }>> {
  const { data } = await client.delete(`/api/v1/copy/symbol-mappings/${id}`);
  return data;
}

/** Toggle a symbol mapping between active and inactive. */
export async function toggleSymbolMapping(
  id: number,
): Promise<ApiResponse<SymbolMapping>> {
  const { data } = await client.put(`/api/v1/copy/symbol-mappings/${id}/toggle`);
  return data;
}

// ── Trade Logs ──────────────────────────────────────

/** Fetch paginated trade logs for a specific relationship. */
export async function fetchTradeLogs(
  relationshipId: number,
  page: number = 1,
  pageSize: number = 50,
): Promise<ApiResponse<PaginatedResponse<CopyTradeLog>>> {
  const { data } = await client.get(
    `/api/v1/copy/relationships/${relationshipId}/logs`,
    { params: { page, page_size: pageSize } },
  );
  return data;
}

// ── Accounts & Brokers (dropdown data) ──────────────

/** Fetch active accounts, optionally filtered by type. */
export async function fetchAccounts(
  accountType?: string,
): Promise<ApiResponse<CopyAccount[]>> {
  const params: Record<string, string> = {};
  if (accountType) params.account_type = accountType;
  const { data } = await client.get('/api/v1/copy/accounts', { params });
  return data;
}

/** Fetch distinct broker names. */
export async function fetchBrokers(): Promise<ApiResponse<CopyBroker[]>> {
  const { data } = await client.get('/api/v1/copy/brokers');
  return data;
}
