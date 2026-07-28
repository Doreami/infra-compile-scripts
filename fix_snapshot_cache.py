path = "/home/xl/iceberg-og/iceberg-rust-bridge/src/services/index/snapshot_cache.rs"
with open(path, "r") as f:
    content = f.read()

# Replace the get_or_load method with the loop-based version
old_method = """    pub async fn get_or_load<F, Fut>(
        &self,
        key: SnapshotCacheKey,
        load_fn: F,
    ) -> Result<Arc<ResolvedMetadataSnapshot>, IndexError>
    where
        F: FnOnce() -> Fut,
        Fut: std::future::Future<Output = Result<ResolvedMetadataSnapshot, IndexError>>,
    {
        // Phase 1: check cache, acquire single-flight slot
        {
            let mut entries = self.entries.lock();
            if let Some(cached) = entries.get(&key) {
                self.counters.cache_hit.fetch_add(1, Ordering::Relaxed);
                return Ok(Arc::clone(cached));
            }
        }

        let slot = {
            let mut slots = self.loading_slots.lock();
            if let Some(existing) = slots.get(&key) {
                let slot = Arc::clone(existing);
                drop(slots);
                self.counters.singleflight_wait.fetch_add(1, Ordering::Relaxed);
                let _guard = slot.lock().await;
                let mut entries = self.entries.lock();
                if let Some(cached) = entries.get(&key) {
                    self.counters.cache_hit.fetch_add(1, Ordering::Relaxed);
                    return Ok(Arc::clone(cached));
                }
                // Leader failed, fall through to become new leader
            }
            let new_slot = Arc::new(TokioMutex::new(()));
            slots.insert(key.clone(), Arc::clone(&new_slot));
            new_slot
        };

        // Phase 2: load (outside any cache lock)
        self.counters.cache_miss.fetch_add(1, Ordering::Relaxed);
        self.counters.load_total.fetch_add(1, Ordering::Relaxed);

        let _slot_guard = slot.lock().await;

        // Double-check after acquiring slot
        {
            let mut entries = self.entries.lock();
            if let Some(cached) = entries.get(&key) {
                self.counters.cache_hit.fetch_add(1, Ordering::Relaxed);
                return Ok(Arc::clone(cached));
            }
        }

        let result = load_fn().await;

        match result {
            Ok(snapshot) => {
                {
                    let mut slots = self.loading_slots.lock();
                    slots.remove(&key);
                }

                if snapshot.approximate_bytes > self.max_entry_bytes {
                    self.counters.bypass_total.fetch_add(1, Ordering::Relaxed);
                    return Ok(Arc::new(snapshot));
                }

                let snapshot = Arc::new(snapshot);

                {
                    let mut entries = self.entries.lock();
                    while entries.len() >= self.max_entries
                        || self.current_bytes.load(Ordering::Relaxed)
                            + snapshot.approximate_bytes as u64
                            > self.max_bytes as u64
                    {
                        if let Some((_k, evicted)) = entries.pop_lru() {
                            self.current_bytes
                                .fetch_sub(evicted.approximate_bytes as u64, Ordering::Relaxed);
                            self.counters.evict_total.fetch_add(1, Ordering::Relaxed);
                        } else {
                            break;
                        }
                    }
                    entries.put(key, Arc::clone(&snapshot));
                    self.current_bytes
                        .fetch_add(snapshot.approximate_bytes as u64, Ordering::Relaxed);
                }

                Ok(snapshot)
            }
            Err(e) => {
                let mut slots = self.loading_slots.lock();
                slots.remove(&key);
                self.counters.load_error.fetch_add(1, Ordering::Relaxed);
                Err(e)
            }
        }
    }"""

new_method = """    pub async fn get_or_load<F, Fut>(
        &self,
        key: SnapshotCacheKey,
        load_fn: F,
    ) -> Result<Arc<ResolvedMetadataSnapshot>, IndexError>
    where
        F: FnOnce() -> Fut,
        Fut: std::future::Future<Output = Result<ResolvedMetadataSnapshot, IndexError>>,
    {
        loop {
            // Check cache first
            {
                let mut entries = self.entries.lock();
                if let Some(cached) = entries.get(&key) {
                    self.counters.cache_hit.fetch_add(1, Ordering::Relaxed);
                    return Ok(Arc::clone(cached));
                }
            }

            // Acquire or wait on single-flight slot
            let slot = {
                let mut slots = self.loading_slots.lock();
                if let Some(existing) = slots.get(&key) {
                    let slot = Arc::clone(existing);
                    drop(slots);
                    self.counters.singleflight_wait.fetch_add(1, Ordering::Relaxed);
                    let _guard = slot.lock().await;
                    // Re-check cache in next loop iteration
                    continue;
                }
                let new_slot = Arc::new(TokioMutex::new(()));
                slots.insert(key.clone(), Arc::clone(&new_slot));
                new_slot
            };

            // We are the leader: load the data
            self.counters.cache_miss.fetch_add(1, Ordering::Relaxed);
            self.counters.load_total.fetch_add(1, Ordering::Relaxed);

            let _slot_guard = slot.lock().await;
            let result = load_fn().await;

            // Remove the loading slot
            {
                let mut slots = self.loading_slots.lock();
                slots.remove(&key);
            }

            return match result {
                Ok(snapshot) => {
                    if snapshot.approximate_bytes > self.max_entry_bytes {
                        self.counters.bypass_total.fetch_add(1, Ordering::Relaxed);
                        Ok(Arc::new(snapshot))
                    } else {
                        let snapshot = Arc::new(snapshot);
                        {
                            let mut entries = self.entries.lock();
                            while entries.len() >= self.max_entries
                                || self.current_bytes.load(Ordering::Relaxed)
                                    + snapshot.approximate_bytes as u64
                                    > self.max_bytes as u64
                            {
                                if let Some((_k, evicted)) = entries.pop_lru() {
                                    self.current_bytes.fetch_sub(
                                        evicted.approximate_bytes as u64,
                                        Ordering::Relaxed,
                                    );
                                    self.counters.evict_total.fetch_add(1, Ordering::Relaxed);
                                } else {
                                    break;
                                }
                            }
                            entries.put(key, Arc::clone(&snapshot));
                            self.current_bytes
                                .fetch_add(snapshot.approximate_bytes as u64, Ordering::Relaxed);
                        }
                        Ok(snapshot)
                    }
                }
                Err(e) => {
                    self.counters.load_error.fetch_add(1, Ordering::Relaxed);
                    Err(e)
                }
            };
        }
    }"""

# Note: The FnOnce is called inside a loop, so it must be FnMut. Let me adjust.
# Actually, the loop only calls load_fn once (we return immediately after).
# But from the compiler's perspective, load_fn is in a loop.
# Let me use FnOnce by extracting the load out of the loop.
# Actually, let me restructure to use Option<F> and take it.

new_method_fixed = """    pub async fn get_or_load<F, Fut>(
        &self,
        key: SnapshotCacheKey,
        load_fn: F,
    ) -> Result<Arc<ResolvedMetadataSnapshot>, IndexError>
    where
        F: FnOnce() -> Fut,
        Fut: std::future::Future<Output = Result<ResolvedMetadataSnapshot, IndexError>>,
    {
        let mut load_fn = Some(load_fn);

        loop {
            // Check cache first
            {
                let mut entries = self.entries.lock();
                if let Some(cached) = entries.get(&key) {
                    self.counters.cache_hit.fetch_add(1, Ordering::Relaxed);
                    return Ok(Arc::clone(cached));
                }
            }

            // Acquire or wait on single-flight slot
            let became_leader = {
                let mut slots = self.loading_slots.lock();
                if let Some(existing) = slots.get(&key) {
                    let slot = Arc::clone(existing);
                    drop(slots);
                    self.counters.singleflight_wait.fetch_add(1, Ordering::Relaxed);
                    let _guard = slot.lock().await;
                    // Re-check cache in next loop iteration
                    continue;
                }
                let new_slot = Arc::new(TokioMutex::new(()));
                slots.insert(key.clone(), Arc::clone(&new_slot));
                new_slot
            };

            // We are the leader: load the data
            self.counters.cache_miss.fetch_add(1, Ordering::Relaxed);
            self.counters.load_total.fetch_add(1, Ordering::Relaxed);

            let _slot_guard = became_leader.lock().await;
            let load = load_fn.take().unwrap();
            let result = load().await;

            // Remove the loading slot
            {
                let mut slots = self.loading_slots.lock();
                slots.remove(&key);
            }

            return match result {
                Ok(snapshot) => {
                    if snapshot.approximate_bytes > self.max_entry_bytes {
                        self.counters.bypass_total.fetch_add(1, Ordering::Relaxed);
                        Ok(Arc::new(snapshot))
                    } else {
                        let snapshot = Arc::new(snapshot);
                        {
                            let mut entries = self.entries.lock();
                            while entries.len() >= self.max_entries
                                || self.current_bytes.load(Ordering::Relaxed)
                                    + snapshot.approximate_bytes as u64
                                    > self.max_bytes as u64
                            {
                                if let Some((_k, evicted)) = entries.pop_lru() {
                                    self.current_bytes.fetch_sub(
                                        evicted.approximate_bytes as u64,
                                        Ordering::Relaxed,
                                    );
                                    self.counters.evict_total.fetch_add(1, Ordering::Relaxed);
                                } else {
                                    break;
                                }
                            }
                            entries.put(key, Arc::clone(&snapshot));
                            self.current_bytes
                                .fetch_add(snapshot.approximate_bytes as u64, Ordering::Relaxed);
                        }
                        Ok(snapshot)
                    }
                }
                Err(e) => {
                    self.counters.load_error.fetch_add(1, Ordering::Relaxed);
                    Err(e)
                }
            };
        }
    }"""

if old_method in content:
    content = content.replace(old_method, new_method_fixed)
    print("Replaced old get_or_load")
else:
    print("WARNING: old method not found")
    # Try to find it
    idx = content.find("pub async fn get_or_load")
    if idx >= 0:
        print(f"Found at index {idx}")
        print(content[idx:idx+200])

with open(path, "w") as f:
    f.write(content)

print("OK: snapshot_cache.rs fixed")
