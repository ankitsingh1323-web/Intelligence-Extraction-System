export type FileCategory =
  | "pdf" | "image" | "csv" | "json" | "excel" | "office" | "email"
  | "database" | "code" | "archive" | "media" | "web" | "unknown";

export type JobStatusValue =
  | "queued" | "parsing" | "extracting" | "graph_build" | "synthesizing" | "complete" | "failed"
  // Job-level only: paused after a batch, waiting for continue/stop.
  | "awaiting_batch_confirm"
  // FileProgress-level only: job was stopped early before this file's batch ran.
  | "skipped";

export interface FileProgress {
  filename: string;
  category: FileCategory;
  status: JobStatusValue;
  error?: string | null;
  warnings: string[];
  detected_language?: string | null;
  translated: boolean;
  entities_found: number;
  relations_found: number;
  pii_found: number;
  financial_facts_found: number;
  tables_found: number;
  chunks_found: number;
  batch?: number | null;
  importance_reason?: string | null;
}

export interface BatchSummary {
  batch_number: number;
  file_count: number;
  top_files: string[];
  entities_found: number;
  relations_found: number;
  pii_found: number;
  financial_facts_found: number;
}

export interface BusinessIndex {
  name: string;
  value: string;
  basis: string;
  sources: string[];
}

export interface ColumnQuality {
  column: string;
  null_rate: number;
  garbage_rate: number;
  distinct_ratio: number;
  type_consistency: number;
  inferred_type: string;
  issues: string[];
}

export interface TableQuality {
  table: string;
  row_count: number;
  duplicate_rows: number;
  duplicate_rate: number;
  columns: ColumnQuality[];
}

export interface DataQuality {
  source_file: string;
  score: number;
  completeness: string;
  issues: string[];
  tables: TableQuality[];
}

export interface FileStats {
  source_file: string;
  category: string;
  entities: number;
  relations: number;
  pii_findings: number;
  financial_facts: number;
  tables: number;
  chunks: number;
  summary?: string | null;
}

export interface BITableColumn {
  target_column: string;
  source_file: string;
  source_table: string;
  source_column: string;
  data_type_guess: string;
  sample_values: string[];
  rule: string;
  comments?: string | null;
}

export interface BITableProposal {
  name: string;
  purpose: string;
  grain: string;
  source_files: string[];
  columns: BITableColumn[];
  join_logic?: string | null;
  join_quality?: string | null;
  target_file: string;
}

export interface FileGroup {
  category: string;
  member_files: string[];
  file_count: number;
  entities: number;
  relations: number;
  pii_findings: number;
  financial_facts: number;
  tables: number;
  chunks: number;
}

export interface BIReport {
  executive_summary: string;
  key_entities: string[];
  financial_highlights: string[];
  risks: string[];
  market_signals: string[];
  corpus_overview: BusinessIndex[];
  file_breakdown: FileStats[];
  file_groups: FileGroup[];
  business_use_cases: BITableProposal[];
  data_quality: DataQuality[];
}

export interface PIIFinding {
  finding_id: string;
  category: string;
  value_redacted: string;
  severity: "low" | "medium" | "high" | "critical";
  source_file: string;
  location?: string | null;
  subject_entity?: string | null;
  detection_method?: "llm" | "rules_checksum" | "rules_shape";
}

export interface ComplianceReport {
  pii_inventory: PIIFinding[];
  severity_counts: Record<string, number>;
  gap_flags: string[];
  remediation: string[];
}

export interface Entity {
  entity_id: string;
  name: string;
  type: string;
  source_file: string;
  mentions: string[];
  confidence: number;
}

export interface Relation {
  relation_id: string;
  source_entity: string;
  target_entity: string;
  relation_type: string;
  source_file: string;
  evidence?: string | null;
  confidence: number;
}

export interface KnowledgeGraphExport {
  entities: Entity[];
  relations: Relation[];
}

export interface TableBlock {
  table_id: string;
  page?: number | null;
  sheet?: string | null;
  headers: string[];
  rows: string[][];
  caption?: string | null;
}

export interface DataDumpTable extends TableBlock {
  source_file: string;
  category: FileCategory;
}

export interface OracleSchemaColumn {
  name: string;
  oracle_type: string;
}

export interface OracleSchemaGroup {
  group_name: string;
  columns: OracleSchemaColumn[];
  member_tables: string[];
  member_files: string[];
  row_count: number;
  combined: boolean;
  union_sql?: string | null;
}

export interface StructuredDataCatalog {
  library_path: string;
  groups: OracleSchemaGroup[];
}

export interface SourceDocSummary {
  source_file: string;
  category: FileCategory;
  has_preview: boolean;
  entities: string[];
  pii_types: string[];
  text_excerpt?: string | null;
}

export interface SourceTargetMapping {
  tables: BITableProposal[];
}

export interface SynthesisOutput {
  bi_report: BIReport;
  compliance_report: ComplianceReport;
  knowledge_graph: KnowledgeGraphExport;
  source_target_mapping: SourceTargetMapping;
  data_dump: { tables: TableBlock[]; files_processed: string[]; chunk_count: number };
}

export type AgentActivityStatus = "running" | "completed" | "failed" | "skipped";

export interface AgentActivity {
  agent: string;
  file: string;
  status: AgentActivityStatus;
  started_at: string;
  finished_at?: string | null;
  duration_ms?: number | null;
}

export interface Job {
  job_id: string;
  name?: string | null;
  status: JobStatusValue;
  created_at: string;
  updated_at: string;
  completed_at?: string | null;
  files: FileProgress[];
  progress_pct: number;
  result: SynthesisOutput | null;
  error?: string | null;
  warnings: string[];
  agent_activity: AgentActivity[];
  total_batches: number;
  current_batch: number;
  batch_summaries: BatchSummary[];
  stopped_early: boolean;
}

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
}

export interface Citation {
  source_file: string;
  chunk_text: string;
  page?: number | null;
  score?: number | null;
}

export interface StructuredQueryResult {
  headers: string[];
  rows: string[][];
  row_count: number;
  truncated: boolean;
}

export interface ChatResponse {
  answer: string;
  citations: Citation[];
  uncertain: boolean;
  degraded: boolean;
  fallback_model?: string | null;
  query_mode?: "graphrag" | "structured";
  sql_used?: string | null;
  structured_result?: StructuredQueryResult | null;
}

export interface OBSCredential {
  id: string;
  name: string;
  endpoint: string;
  region?: string | null;
  bucket: string;
  path_prefix: string;
  access_key: string;
  secret_key_hint: string;
  is_active: boolean;
  created_at: string;
  verified_at?: string | null;
  verified_ok?: boolean | null;
  verified_detail?: string | null;
}

export interface OBSSettings {
  enabled: boolean;
  credentials: OBSCredential[];
}

export interface OBSCredentialInput {
  name: string;
  endpoint: string;
  region?: string;
  bucket: string;
  path_prefix?: string;
  access_key: string;
  secret_key: string;
}

export interface OBSTestResult {
  ok: boolean;
  detail: string;
}

export interface OBSObjectSummary {
  key: string;
  size: number;
  last_modified: string;
}

export interface OBSBrowseResult {
  objects: OBSObjectSummary[];
  truncated: boolean;
}
