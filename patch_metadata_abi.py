path = "/home/xl/iceberg-og/iceberg-rust-bridge/src/services/index/metadata_abi.rs"
with open(path, "r") as f:
    content = f.read()

# --- Add imports for cache types ---
old_import = """use iceberg_index_abi::{
    AsyncScanCursor, DistanceType, IndexError, IndexType, MetadataBuildRequest,
    MetadataDropRequest, MetadataIndexService, MetadataMatchRequest, MetadataRegistryRequest,
    MetadataScalarSearchRequest, MetadataSearchRequest, PreparedStatisticsResult,
    StagedBuildHandle,
};"""

new_import = """use iceberg_index_abi::{
    AsyncScanCursor, DistanceType, IndexError, IndexType, MetadataBuildRequest,
    MetadataDropRequest, MetadataIndexService, MetadataMatchRequest, MetadataRegistryRequest,
    MetadataScalarSearchRequest, MetadataSearchRequest, PreparedStatisticsResult,
    SnapshotCacheKey, StagedBuildHandle,
};

use crate::services::index::snapshot_cache::get_snapshot_cache;"""

content = content.replace(old_import, new_import)

# --- Modify match_index_by_metadata ---

old_match_block = """        let sdk_req = MetadataMatchRequest {
            table_namespace,
            table_name: table_name.to_string(),
            metadata_location: metadata_location.to_string(),
            index_name,
            file_io_config_json,
        };

        match bridge_block_on(service.match_index_by_metadata(&sdk_req)) {
            Ok(Some(json)) => match IcebergBridgeString::new(json) {
                Ok(s) => set_out(out_result, s),
                Err(code) => set_error(err, code, "failed to allocate result string", ctx),
            },
            Ok(None) => IcebergBridgeStatus::Ok,
            Err(e) => set_index_error(err, &e, ctx),
        }"""

new_match_block = """        // Resolve metadata via process-level snapshot cache
        let cache_key = SnapshotCacheKey {
            scope_hash: match valid_storage(storage) {
                Some(s) => s.scope.scope_hash(),
                None => return set_error(
                    err,
                    IcebergBridgeStatus::InvalidArgument,
                    "invalid storage handle",
                    ctx,
                ),
            },
            table_namespace: table_namespace.clone(),
            table_name: table_name.to_string(),
            metadata_location: metadata_location.to_string(),
        };

        let snapshot = bridge_block_on(async {
            get_snapshot_cache()
                .get_or_load(cache_key, {
                    let service = Arc::clone(&service);
                    let ns = table_namespace.clone();
                    let name = table_name.to_string();
                    let loc = metadata_location.to_string();
                    let cfg = file_io_config_json.clone();
                    async move {
                        service
                            .resolve_metadata_snapshot(ns, name, loc, &cfg)
                            .await
                    }
                })
                .await
        });
        let snapshot = match snapshot {
            Ok(s) => s,
            Err(e) => return set_index_error(err, &e, ctx),
        };

        let view = match bridge_block_on(
            service.bind_resolved_snapshot(&snapshot, &file_io_config_json),
        ) {
            Ok(v) => v,
            Err(e) => return set_index_error(err, &e, ctx),
        };

        let sdk_req = MetadataMatchRequest {
            table_namespace,
            table_name: table_name.to_string(),
            metadata_location: metadata_location.to_string(),
            index_name,
            file_io_config_json,
        };

        match bridge_block_on(service.match_index_by_metadata_with_view(&sdk_req, &view)) {
            Ok(Some(json)) => match IcebergBridgeString::new(json) {
                Ok(s) => set_out(out_result, s),
                Err(code) => set_error(err, code, "failed to allocate result string", ctx),
            },
            Ok(None) => IcebergBridgeStatus::Ok,
            Err(e) => set_index_error(err, &e, ctx),
        }"""

content = content.replace(old_match_block, new_match_block)

# --- Modify search_vector_by_metadata ---

old_search_block = """        let sdk_req = MetadataSearchRequest {
            table_namespace,
            table_name: table_name.to_string(),
            metadata_location: metadata_location.to_string(),
            index_name: index_name.to_string(),
            query_vector,
            k: req.k,
            distance_type,
            params_json: params_json.to_string(),
            file_io_config_json,
        };

        match bridge_block_on(service.search_vector_by_metadata(&sdk_req)) {
            Ok(cursor) => set_out(out_scan, Box::new(IcebergIndexScan::new(cursor))),
            Err(e) => set_index_error(err, &e, ctx),
        }"""

new_search_block = """        // Resolve metadata via process-level snapshot cache
        let cache_key = SnapshotCacheKey {
            scope_hash: match valid_storage(storage) {
                Some(s) => s.scope.scope_hash(),
                None => return set_error(
                    err,
                    IcebergBridgeStatus::InvalidArgument,
                    "invalid storage handle",
                    ctx,
                ),
            },
            table_namespace: table_namespace.clone(),
            table_name: table_name.to_string(),
            metadata_location: metadata_location.to_string(),
        };

        let snapshot = bridge_block_on(async {
            get_snapshot_cache()
                .get_or_load(cache_key, {
                    let service = Arc::clone(&service);
                    let ns = table_namespace.clone();
                    let name = table_name.to_string();
                    let loc = metadata_location.to_string();
                    let cfg = file_io_config_json.clone();
                    async move {
                        service
                            .resolve_metadata_snapshot(ns, name, loc, &cfg)
                            .await
                    }
                })
                .await
        });
        let snapshot = match snapshot {
            Ok(s) => s,
            Err(e) => return set_index_error(err, &e, ctx),
        };

        let view = match bridge_block_on(
            service.bind_resolved_snapshot(&snapshot, &file_io_config_json),
        ) {
            Ok(v) => v,
            Err(e) => return set_index_error(err, &e, ctx),
        };

        let sdk_req = MetadataSearchRequest {
            table_namespace,
            table_name: table_name.to_string(),
            metadata_location: metadata_location.to_string(),
            index_name: index_name.to_string(),
            query_vector,
            k: req.k,
            distance_type,
            params_json: params_json.to_string(),
            file_io_config_json,
        };

        match bridge_block_on(service.search_vector_by_metadata_with_view(&sdk_req, &view)) {
            Ok(cursor) => set_out(out_scan, Box::new(IcebergIndexScan::new(cursor))),
            Err(e) => set_index_error(err, &e, ctx),
        }"""

content = content.replace(old_search_block, new_search_block)

# --- Modify search_scalar_by_metadata ---

old_scalar_block = """        let sdk_req = MetadataScalarSearchRequest {
            table_namespace,
            table_name: table_name.to_string(),
            metadata_location: metadata_location.to_string(),
            index_name: index_name.to_string(),
            expression_json: expression_json.to_string(),
            projection_columns,
            file_io_config_json,
        };

        match bridge_block_on(service.search_scalar_by_metadata(&sdk_req)) {
            Ok(cursor) => set_out(out_scan, Box::new(IcebergIndexScan::new(cursor))),
            Err(e) => set_index_error(err, &e, ctx),
        }"""

new_scalar_block = """        // Resolve metadata via process-level snapshot cache
        let cache_key = SnapshotCacheKey {
            scope_hash: match valid_storage(storage) {
                Some(s) => s.scope.scope_hash(),
                None => return set_error(
                    err,
                    IcebergBridgeStatus::InvalidArgument,
                    "invalid storage handle",
                    ctx,
                ),
            },
            table_namespace: table_namespace.clone(),
            table_name: table_name.to_string(),
            metadata_location: metadata_location.to_string(),
        };

        let snapshot = bridge_block_on(async {
            get_snapshot_cache()
                .get_or_load(cache_key, {
                    let service = Arc::clone(&service);
                    let ns = table_namespace.clone();
                    let name = table_name.to_string();
                    let loc = metadata_location.to_string();
                    let cfg = file_io_config_json.clone();
                    async move {
                        service
                            .resolve_metadata_snapshot(ns, name, loc, &cfg)
                            .await
                    }
                })
                .await
        });
        let snapshot = match snapshot {
            Ok(s) => s,
            Err(e) => return set_index_error(err, &e, ctx),
        };

        let view = match bridge_block_on(
            service.bind_resolved_snapshot(&snapshot, &file_io_config_json),
        ) {
            Ok(v) => v,
            Err(e) => return set_index_error(err, &e, ctx),
        };

        let sdk_req = MetadataScalarSearchRequest {
            table_namespace,
            table_name: table_name.to_string(),
            metadata_location: metadata_location.to_string(),
            index_name: index_name.to_string(),
            expression_json: expression_json.to_string(),
            projection_columns,
            file_io_config_json,
        };

        match bridge_block_on(service.search_scalar_by_metadata_with_view(&sdk_req, &view)) {
            Ok(cursor) => set_out(out_scan, Box::new(IcebergIndexScan::new(cursor))),
            Err(e) => set_index_error(err, &e, ctx),
        }"""

content = content.replace(old_scalar_block, new_scalar_block)

with open(path, "w") as f:
    f.write(content)

print("OK: metadata_abi.rs wired with cache")
