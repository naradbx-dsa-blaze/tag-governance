import { useQuery, useSuspenseQuery, useMutation } from "@tanstack/react-query";
import type { UseQueryOptions, UseSuspenseQueryOptions, UseMutationOptions } from "@tanstack/react-query";
export class ApiError extends Error {
    status: number;
    statusText: string;
    body: unknown;
    constructor(status: number, statusText: string, body: unknown){
        super(`HTTP ${status}: ${statusText}`);
        this.name = "ApiError";
        this.status = status;
        this.statusText = statusText;
        this.body = body;
    }
}
export interface AutoTagBody {
    days?: number;
    dry_run?: boolean;
    min_confidence?: number;
    rules?: Record<string, unknown>[] | null;
    tag_key?: string;
    use_ai?: boolean;
}
export interface BatchBody {
    batch_id: string;
    dry_run?: boolean;
}
export interface BatchesOut {
    batches: Record<string, unknown>[];
}
export interface HTTPValidationError {
    detail?: ValidationError[];
}
export interface HealthOut {
    detail?: string | null;
    ok: boolean;
    warehouse_id?: string | null;
}
export interface ManualTagBody {
    dry_run?: boolean;
    is_serverless?: boolean;
    list_cost?: number | null;
    product: string;
    tag_key?: string;
    tag_value: string;
    workload_id: string;
    workload_name?: string;
    workspace_id?: string;
}
export interface OverviewOut {
    days: number;
    kpi: Record<string, unknown>;
    products: Record<string, unknown>[];
    tag_key: string;
}
export interface PreviewOut {
    excluded: Record<string, unknown>;
    impact: Record<string, unknown>;
    workloads: Record<string, unknown>[];
}
export interface RowsOut {
    rows: Record<string, unknown>[];
}
export interface RulePreviewBody {
    days?: number;
    rules: Record<string, unknown>[];
    tag_key?: string;
}
export interface RulePreviewOut {
    impact: Record<string, unknown>;
    workloads: Record<string, unknown>[];
}
export interface RunOut {
    ai_rows?: number | null;
    batch_id?: string | null;
    error?: string | null;
    message?: string | null;
    rule_rows?: number | null;
    run?: Record<string, unknown> | null;
    status?: string | null;
    total_rows?: number | null;
}
export interface TagSelectedBody {
    dry_run?: boolean;
    tag_key?: string;
    workloads: Record<string, unknown>[];
}
export interface ValidationError {
    ctx?: Record<string, unknown>;
    input?: unknown;
    loc: (string | number)[];
    msg: string;
    type: string;
}
export interface ValuesOut {
    values: Record<string, unknown>[];
}
export interface VersionOut {
    version: string;
}
export interface WhoAmIOut {
    admin_group: string;
    can_write: boolean;
    display_name?: string | null;
    email: string;
    reason: string;
}
export const autoTag = async (data: AutoTagBody, options?: RequestInit): Promise<{
    data: RunOut;
}> =>{
    const res = await fetch("/api/auto-tag", {
        ...options,
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            ...options?.headers
        },
        body: JSON.stringify(data)
    });
    if (!res.ok) {
        const body = await res.text();
        let parsed: unknown;
        try {
            parsed = JSON.parse(body);
        } catch  {
            parsed = body;
        }
        throw new ApiError(res.status, res.statusText, parsed);
    }
    return {
        data: await res.json()
    };
};
export function useAutoTag(options?: {
    mutation?: UseMutationOptions<{
        data: RunOut;
    }, ApiError, AutoTagBody>;
}) {
    return useMutation({
        mutationFn: (data)=>autoTag(data),
        ...options?.mutation
    });
}
export interface BatchDetailParams {
    batch_id: string;
}
export const batchDetail = async (params: BatchDetailParams, options?: RequestInit): Promise<{
    data: RowsOut;
}> =>{
    const searchParams = new URLSearchParams();
    if (params.batch_id != null) searchParams.set("batch_id", String(params.batch_id));
    const queryString = searchParams.toString();
    const url = queryString ? `/api/batch-detail?${queryString}` : "/api/batch-detail";
    const res = await fetch(url, {
        ...options,
        method: "GET"
    });
    if (!res.ok) {
        const body = await res.text();
        let parsed: unknown;
        try {
            parsed = JSON.parse(body);
        } catch  {
            parsed = body;
        }
        throw new ApiError(res.status, res.statusText, parsed);
    }
    return {
        data: await res.json()
    };
};
export const batchDetailKey = (params?: BatchDetailParams)=>{
    return [
        "/api/batch-detail",
        params
    ] as const;
};
export function useBatchDetail<TData = {
    data: RowsOut;
}>(options: {
    params: BatchDetailParams;
    query?: Omit<UseQueryOptions<{
        data: RowsOut;
    }, ApiError, TData>, "queryKey" | "queryFn">;
}) {
    return useQuery({
        queryKey: batchDetailKey(options.params),
        queryFn: ()=>batchDetail(options.params),
        ...options?.query
    });
}
export function useBatchDetailSuspense<TData = {
    data: RowsOut;
}>(options: {
    params: BatchDetailParams;
    query?: Omit<UseSuspenseQueryOptions<{
        data: RowsOut;
    }, ApiError, TData>, "queryKey" | "queryFn">;
}) {
    return useSuspenseQuery({
        queryKey: batchDetailKey(options.params),
        queryFn: ()=>batchDetail(options.params),
        ...options?.query
    });
}
export interface BatchesParams {
    limit?: number;
}
export const batches = async (params?: BatchesParams, options?: RequestInit): Promise<{
    data: BatchesOut;
}> =>{
    const searchParams = new URLSearchParams();
    if (params?.limit != null) searchParams.set("limit", String(params?.limit));
    const queryString = searchParams.toString();
    const url = queryString ? `/api/batches?${queryString}` : "/api/batches";
    const res = await fetch(url, {
        ...options,
        method: "GET"
    });
    if (!res.ok) {
        const body = await res.text();
        let parsed: unknown;
        try {
            parsed = JSON.parse(body);
        } catch  {
            parsed = body;
        }
        throw new ApiError(res.status, res.statusText, parsed);
    }
    return {
        data: await res.json()
    };
};
export const batchesKey = (params?: BatchesParams)=>{
    return [
        "/api/batches",
        params
    ] as const;
};
export function useBatches<TData = {
    data: BatchesOut;
}>(options?: {
    params?: BatchesParams;
    query?: Omit<UseQueryOptions<{
        data: BatchesOut;
    }, ApiError, TData>, "queryKey" | "queryFn">;
}) {
    return useQuery({
        queryKey: batchesKey(options?.params),
        queryFn: ()=>batches(options?.params),
        ...options?.query
    });
}
export function useBatchesSuspense<TData = {
    data: BatchesOut;
}>(options?: {
    params?: BatchesParams;
    query?: Omit<UseSuspenseQueryOptions<{
        data: BatchesOut;
    }, ApiError, TData>, "queryKey" | "queryFn">;
}) {
    return useSuspenseQuery({
        queryKey: batchesKey(options?.params),
        queryFn: ()=>batches(options?.params),
        ...options?.query
    });
}
export const capabilities = async (options?: RequestInit): Promise<{
    data: RowsOut;
}> =>{
    const res = await fetch("/api/capabilities", {
        ...options,
        method: "GET"
    });
    if (!res.ok) {
        const body = await res.text();
        let parsed: unknown;
        try {
            parsed = JSON.parse(body);
        } catch  {
            parsed = body;
        }
        throw new ApiError(res.status, res.statusText, parsed);
    }
    return {
        data: await res.json()
    };
};
export const capabilitiesKey = ()=>{
    return [
        "/api/capabilities"
    ] as const;
};
export function useCapabilities<TData = {
    data: RowsOut;
}>(options?: {
    query?: Omit<UseQueryOptions<{
        data: RowsOut;
    }, ApiError, TData>, "queryKey" | "queryFn">;
}) {
    return useQuery({
        queryKey: capabilitiesKey(),
        queryFn: ()=>capabilities(),
        ...options?.query
    });
}
export function useCapabilitiesSuspense<TData = {
    data: RowsOut;
}>(options?: {
    query?: Omit<UseSuspenseQueryOptions<{
        data: RowsOut;
    }, ApiError, TData>, "queryKey" | "queryFn">;
}) {
    return useSuspenseQuery({
        queryKey: capabilitiesKey(),
        queryFn: ()=>capabilities(),
        ...options?.query
    });
}
export interface FieldValuesParams {
    field?: string;
    days?: number;
}
export const fieldValues = async (params?: FieldValuesParams, options?: RequestInit): Promise<{
    data: ValuesOut;
}> =>{
    const searchParams = new URLSearchParams();
    if (params?.field != null) searchParams.set("field", String(params?.field));
    if (params?.days != null) searchParams.set("days", String(params?.days));
    const queryString = searchParams.toString();
    const url = queryString ? `/api/field-values?${queryString}` : "/api/field-values";
    const res = await fetch(url, {
        ...options,
        method: "GET"
    });
    if (!res.ok) {
        const body = await res.text();
        let parsed: unknown;
        try {
            parsed = JSON.parse(body);
        } catch  {
            parsed = body;
        }
        throw new ApiError(res.status, res.statusText, parsed);
    }
    return {
        data: await res.json()
    };
};
export const fieldValuesKey = (params?: FieldValuesParams)=>{
    return [
        "/api/field-values",
        params
    ] as const;
};
export function useFieldValues<TData = {
    data: ValuesOut;
}>(options?: {
    params?: FieldValuesParams;
    query?: Omit<UseQueryOptions<{
        data: ValuesOut;
    }, ApiError, TData>, "queryKey" | "queryFn">;
}) {
    return useQuery({
        queryKey: fieldValuesKey(options?.params),
        queryFn: ()=>fieldValues(options?.params),
        ...options?.query
    });
}
export function useFieldValuesSuspense<TData = {
    data: ValuesOut;
}>(options?: {
    params?: FieldValuesParams;
    query?: Omit<UseSuspenseQueryOptions<{
        data: ValuesOut;
    }, ApiError, TData>, "queryKey" | "queryFn">;
}) {
    return useSuspenseQuery({
        queryKey: fieldValuesKey(options?.params),
        queryFn: ()=>fieldValues(options?.params),
        ...options?.query
    });
}
export const health = async (options?: RequestInit): Promise<{
    data: HealthOut;
}> =>{
    const res = await fetch("/api/health", {
        ...options,
        method: "GET"
    });
    if (!res.ok) {
        const body = await res.text();
        let parsed: unknown;
        try {
            parsed = JSON.parse(body);
        } catch  {
            parsed = body;
        }
        throw new ApiError(res.status, res.statusText, parsed);
    }
    return {
        data: await res.json()
    };
};
export const healthKey = ()=>{
    return [
        "/api/health"
    ] as const;
};
export function useHealth<TData = {
    data: HealthOut;
}>(options?: {
    query?: Omit<UseQueryOptions<{
        data: HealthOut;
    }, ApiError, TData>, "queryKey" | "queryFn">;
}) {
    return useQuery({
        queryKey: healthKey(),
        queryFn: ()=>health(),
        ...options?.query
    });
}
export function useHealthSuspense<TData = {
    data: HealthOut;
}>(options?: {
    query?: Omit<UseSuspenseQueryOptions<{
        data: HealthOut;
    }, ApiError, TData>, "queryKey" | "queryFn">;
}) {
    return useSuspenseQuery({
        queryKey: healthKey(),
        queryFn: ()=>health(),
        ...options?.query
    });
}
export const inventory = async (options?: RequestInit): Promise<{
    data: RowsOut;
}> =>{
    const res = await fetch("/api/inventory", {
        ...options,
        method: "GET"
    });
    if (!res.ok) {
        const body = await res.text();
        let parsed: unknown;
        try {
            parsed = JSON.parse(body);
        } catch  {
            parsed = body;
        }
        throw new ApiError(res.status, res.statusText, parsed);
    }
    return {
        data: await res.json()
    };
};
export const inventoryKey = ()=>{
    return [
        "/api/inventory"
    ] as const;
};
export function useInventory<TData = {
    data: RowsOut;
}>(options?: {
    query?: Omit<UseQueryOptions<{
        data: RowsOut;
    }, ApiError, TData>, "queryKey" | "queryFn">;
}) {
    return useQuery({
        queryKey: inventoryKey(),
        queryFn: ()=>inventory(),
        ...options?.query
    });
}
export function useInventorySuspense<TData = {
    data: RowsOut;
}>(options?: {
    query?: Omit<UseSuspenseQueryOptions<{
        data: RowsOut;
    }, ApiError, TData>, "queryKey" | "queryFn">;
}) {
    return useSuspenseQuery({
        queryKey: inventoryKey(),
        queryFn: ()=>inventory(),
        ...options?.query
    });
}
export const manualTag = async (data: ManualTagBody, options?: RequestInit): Promise<{
    data: RunOut;
}> =>{
    const res = await fetch("/api/manual-tag", {
        ...options,
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            ...options?.headers
        },
        body: JSON.stringify(data)
    });
    if (!res.ok) {
        const body = await res.text();
        let parsed: unknown;
        try {
            parsed = JSON.parse(body);
        } catch  {
            parsed = body;
        }
        throw new ApiError(res.status, res.statusText, parsed);
    }
    return {
        data: await res.json()
    };
};
export function useManualTag(options?: {
    mutation?: UseMutationOptions<{
        data: RunOut;
    }, ApiError, ManualTagBody>;
}) {
    return useMutation({
        mutationFn: (data)=>manualTag(data),
        ...options?.mutation
    });
}
export interface NotTaggableParams {
    days?: number;
    tag_key?: string;
}
export const notTaggable = async (params?: NotTaggableParams, options?: RequestInit): Promise<{
    data: RowsOut;
}> =>{
    const searchParams = new URLSearchParams();
    if (params?.days != null) searchParams.set("days", String(params?.days));
    if (params?.tag_key != null) searchParams.set("tag_key", String(params?.tag_key));
    const queryString = searchParams.toString();
    const url = queryString ? `/api/not-taggable?${queryString}` : "/api/not-taggable";
    const res = await fetch(url, {
        ...options,
        method: "GET"
    });
    if (!res.ok) {
        const body = await res.text();
        let parsed: unknown;
        try {
            parsed = JSON.parse(body);
        } catch  {
            parsed = body;
        }
        throw new ApiError(res.status, res.statusText, parsed);
    }
    return {
        data: await res.json()
    };
};
export const notTaggableKey = (params?: NotTaggableParams)=>{
    return [
        "/api/not-taggable",
        params
    ] as const;
};
export function useNotTaggable<TData = {
    data: RowsOut;
}>(options?: {
    params?: NotTaggableParams;
    query?: Omit<UseQueryOptions<{
        data: RowsOut;
    }, ApiError, TData>, "queryKey" | "queryFn">;
}) {
    return useQuery({
        queryKey: notTaggableKey(options?.params),
        queryFn: ()=>notTaggable(options?.params),
        ...options?.query
    });
}
export function useNotTaggableSuspense<TData = {
    data: RowsOut;
}>(options?: {
    params?: NotTaggableParams;
    query?: Omit<UseSuspenseQueryOptions<{
        data: RowsOut;
    }, ApiError, TData>, "queryKey" | "queryFn">;
}) {
    return useSuspenseQuery({
        queryKey: notTaggableKey(options?.params),
        queryFn: ()=>notTaggable(options?.params),
        ...options?.query
    });
}
export interface OverviewParams {
    days?: number;
    tag_key?: string;
}
export const overview = async (params?: OverviewParams, options?: RequestInit): Promise<{
    data: OverviewOut;
}> =>{
    const searchParams = new URLSearchParams();
    if (params?.days != null) searchParams.set("days", String(params?.days));
    if (params?.tag_key != null) searchParams.set("tag_key", String(params?.tag_key));
    const queryString = searchParams.toString();
    const url = queryString ? `/api/overview?${queryString}` : "/api/overview";
    const res = await fetch(url, {
        ...options,
        method: "GET"
    });
    if (!res.ok) {
        const body = await res.text();
        let parsed: unknown;
        try {
            parsed = JSON.parse(body);
        } catch  {
            parsed = body;
        }
        throw new ApiError(res.status, res.statusText, parsed);
    }
    return {
        data: await res.json()
    };
};
export const overviewKey = (params?: OverviewParams)=>{
    return [
        "/api/overview",
        params
    ] as const;
};
export function useOverview<TData = {
    data: OverviewOut;
}>(options?: {
    params?: OverviewParams;
    query?: Omit<UseQueryOptions<{
        data: OverviewOut;
    }, ApiError, TData>, "queryKey" | "queryFn">;
}) {
    return useQuery({
        queryKey: overviewKey(options?.params),
        queryFn: ()=>overview(options?.params),
        ...options?.query
    });
}
export function useOverviewSuspense<TData = {
    data: OverviewOut;
}>(options?: {
    params?: OverviewParams;
    query?: Omit<UseSuspenseQueryOptions<{
        data: OverviewOut;
    }, ApiError, TData>, "queryKey" | "queryFn">;
}) {
    return useSuspenseQuery({
        queryKey: overviewKey(options?.params),
        queryFn: ()=>overview(options?.params),
        ...options?.query
    });
}
export interface AiPreviewParams {
    days?: number;
    tag_key?: string;
    min_confidence?: number;
}
export const aiPreview = async (params?: AiPreviewParams, options?: RequestInit): Promise<{
    data: PreviewOut;
}> =>{
    const searchParams = new URLSearchParams();
    if (params?.days != null) searchParams.set("days", String(params?.days));
    if (params?.tag_key != null) searchParams.set("tag_key", String(params?.tag_key));
    if (params?.min_confidence != null) searchParams.set("min_confidence", String(params?.min_confidence));
    const queryString = searchParams.toString();
    const url = queryString ? `/api/preview?${queryString}` : "/api/preview";
    const res = await fetch(url, {
        ...options,
        method: "GET"
    });
    if (!res.ok) {
        const body = await res.text();
        let parsed: unknown;
        try {
            parsed = JSON.parse(body);
        } catch  {
            parsed = body;
        }
        throw new ApiError(res.status, res.statusText, parsed);
    }
    return {
        data: await res.json()
    };
};
export const aiPreviewKey = (params?: AiPreviewParams)=>{
    return [
        "/api/preview",
        params
    ] as const;
};
export function useAiPreview<TData = {
    data: PreviewOut;
}>(options?: {
    params?: AiPreviewParams;
    query?: Omit<UseQueryOptions<{
        data: PreviewOut;
    }, ApiError, TData>, "queryKey" | "queryFn">;
}) {
    return useQuery({
        queryKey: aiPreviewKey(options?.params),
        queryFn: ()=>aiPreview(options?.params),
        ...options?.query
    });
}
export function useAiPreviewSuspense<TData = {
    data: PreviewOut;
}>(options?: {
    params?: AiPreviewParams;
    query?: Omit<UseSuspenseQueryOptions<{
        data: PreviewOut;
    }, ApiError, TData>, "queryKey" | "queryFn">;
}) {
    return useSuspenseQuery({
        queryKey: aiPreviewKey(options?.params),
        queryFn: ()=>aiPreview(options?.params),
        ...options?.query
    });
}
export const rollback = async (data: BatchBody, options?: RequestInit): Promise<{
    data: RunOut;
}> =>{
    const res = await fetch("/api/rollback", {
        ...options,
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            ...options?.headers
        },
        body: JSON.stringify(data)
    });
    if (!res.ok) {
        const body = await res.text();
        let parsed: unknown;
        try {
            parsed = JSON.parse(body);
        } catch  {
            parsed = body;
        }
        throw new ApiError(res.status, res.statusText, parsed);
    }
    return {
        data: await res.json()
    };
};
export function useRollback(options?: {
    mutation?: UseMutationOptions<{
        data: RunOut;
    }, ApiError, BatchBody>;
}) {
    return useMutation({
        mutationFn: (data)=>rollback(data),
        ...options?.mutation
    });
}
export const rulePreview = async (data: RulePreviewBody, options?: RequestInit): Promise<{
    data: RulePreviewOut;
}> =>{
    const res = await fetch("/api/rule-preview", {
        ...options,
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            ...options?.headers
        },
        body: JSON.stringify(data)
    });
    if (!res.ok) {
        const body = await res.text();
        let parsed: unknown;
        try {
            parsed = JSON.parse(body);
        } catch  {
            parsed = body;
        }
        throw new ApiError(res.status, res.statusText, parsed);
    }
    return {
        data: await res.json()
    };
};
export function useRulePreview(options?: {
    mutation?: UseMutationOptions<{
        data: RulePreviewOut;
    }, ApiError, RulePreviewBody>;
}) {
    return useMutation({
        mutationFn: (data)=>rulePreview(data),
        ...options?.mutation
    });
}
export const tagSelected = async (data: TagSelectedBody, options?: RequestInit): Promise<{
    data: RunOut;
}> =>{
    const res = await fetch("/api/tag-selected", {
        ...options,
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            ...options?.headers
        },
        body: JSON.stringify(data)
    });
    if (!res.ok) {
        const body = await res.text();
        let parsed: unknown;
        try {
            parsed = JSON.parse(body);
        } catch  {
            parsed = body;
        }
        throw new ApiError(res.status, res.statusText, parsed);
    }
    return {
        data: await res.json()
    };
};
export function useTagSelected(options?: {
    mutation?: UseMutationOptions<{
        data: RunOut;
    }, ApiError, TagSelectedBody>;
}) {
    return useMutation({
        mutationFn: (data)=>tagSelected(data),
        ...options?.mutation
    });
}
export const version = async (options?: RequestInit): Promise<{
    data: VersionOut;
}> =>{
    const res = await fetch("/api/version", {
        ...options,
        method: "GET"
    });
    if (!res.ok) {
        const body = await res.text();
        let parsed: unknown;
        try {
            parsed = JSON.parse(body);
        } catch  {
            parsed = body;
        }
        throw new ApiError(res.status, res.statusText, parsed);
    }
    return {
        data: await res.json()
    };
};
export const versionKey = ()=>{
    return [
        "/api/version"
    ] as const;
};
export function useVersion<TData = {
    data: VersionOut;
}>(options?: {
    query?: Omit<UseQueryOptions<{
        data: VersionOut;
    }, ApiError, TData>, "queryKey" | "queryFn">;
}) {
    return useQuery({
        queryKey: versionKey(),
        queryFn: ()=>version(),
        ...options?.query
    });
}
export function useVersionSuspense<TData = {
    data: VersionOut;
}>(options?: {
    query?: Omit<UseSuspenseQueryOptions<{
        data: VersionOut;
    }, ApiError, TData>, "queryKey" | "queryFn">;
}) {
    return useSuspenseQuery({
        queryKey: versionKey(),
        queryFn: ()=>version(),
        ...options?.query
    });
}
export const whoami = async (options?: RequestInit): Promise<{
    data: WhoAmIOut;
}> =>{
    const res = await fetch("/api/whoami", {
        ...options,
        method: "GET"
    });
    if (!res.ok) {
        const body = await res.text();
        let parsed: unknown;
        try {
            parsed = JSON.parse(body);
        } catch  {
            parsed = body;
        }
        throw new ApiError(res.status, res.statusText, parsed);
    }
    return {
        data: await res.json()
    };
};
export const whoamiKey = ()=>{
    return [
        "/api/whoami"
    ] as const;
};
export function useWhoami<TData = {
    data: WhoAmIOut;
}>(options?: {
    query?: Omit<UseQueryOptions<{
        data: WhoAmIOut;
    }, ApiError, TData>, "queryKey" | "queryFn">;
}) {
    return useQuery({
        queryKey: whoamiKey(),
        queryFn: ()=>whoami(),
        ...options?.query
    });
}
export function useWhoamiSuspense<TData = {
    data: WhoAmIOut;
}>(options?: {
    query?: Omit<UseSuspenseQueryOptions<{
        data: WhoAmIOut;
    }, ApiError, TData>, "queryKey" | "queryFn">;
}) {
    return useSuspenseQuery({
        queryKey: whoamiKey(),
        queryFn: ()=>whoami(),
        ...options?.query
    });
}
