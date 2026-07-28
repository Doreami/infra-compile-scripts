path = "/home/xl/iceberg-og/iceberg-rust-bridge/src/services/index/metadata_abi.rs"
with open(path, "r") as f:
    content = f.read()

# Fix: the get_or_load call passes an async block instead of a closure.
# We need to wrap each with || async { ... }
# The pattern is: get_or_load(cache_key, { ... }).await
# Should be: get_or_load(cache_key, || async { ... }).await

# Find all instances of: .get_or_load(cache_key, {
# and replace with: .get_or_load(cache_key, || {
# The async move { ... } block is already there, we just need to add ||

old_pattern = """.get_or_load(cache_key, {
                    let service = Arc::clone(&service);"""
new_pattern = """.get_or_load(cache_key, || {
                    let service = Arc::clone(&service);"""

# This pattern appears 3 times
count = content.count(old_pattern)
print(f"Found {count} occurrences")

content = content.replace(old_pattern, new_pattern)

# Now also fix the closing of get_or_load: }).await -> }}).await
# The pattern is:
#                 })
#                 .await
# Should become:
#                 }})
#                 .await
# But we need to be careful about which }) to replace

# Actually, the original get_or_load closure block ends with:
#                 })
#                 .await
# After our change, it should end with:
#                 }})
#                 .await

# Let me find the pattern: the last }) before .await in each cache block
# The pattern is:
#                     async move {
#                         service
#                             .resolve_metadata_snapshot(ns, name, loc, &cfg)
#                             .await
#                     }
#                 })
#                 .await

old_end = """                    async move {
                        service
                            .resolve_metadata_snapshot(ns, name, loc, &cfg)
                            .await
                    }
                })
                .await"""

new_end = """                    async move {
                        service
                            .resolve_metadata_snapshot(ns, name, loc, &cfg)
                            .await
                    }
                }})
                .await"""

count2 = content.count(old_end)
print(f"Found {count2} end occurrences")

content = content.replace(old_end, new_end)

with open(path, "w") as f:
    f.write(content)

print("OK: metadata_abi.rs fixed")
