path = "/home/xl/iceberg-og/iceberg-rust-bridge/src/services/index/metadata_abi.rs"
with open(path, "r") as f:
    content = f.read()

# The issue: get_or_load takes FnOnce() -> Fut, but we pass an async block directly.
# Fix: change `get_or_load(cache_key, {` to `get_or_load(cache_key, || {`
# The closing remains `})` (which closes both `|| {` and `get_or_load(`)

# But my previous fix changed `})` to `}})` which broke the braces.
# Let me revert `}})` back to `})` first.

content = content.replace("                }})\n                .await", "                })\n                .await")

# Now change `get_or_load(cache_key, {` to `get_or_load(cache_key, || {`
# But only where it's NOT already `|| {`
content = content.replace(".get_or_load(cache_key, || {", ".get_or_load(cache_key, {")  # undo previous fix
content = content.replace(".get_or_load(cache_key, {", ".get_or_load(cache_key, || {")

with open(path, "w") as f:
    f.write(content)

# Verify
with open(path, "r") as f:
    verify = f.read()

count_or_load = verify.count(".get_or_load(cache_key, || {")
count_bad = verify.count(".get_or_load(cache_key, {")
count_braces = verify.count("}})\n                .await")

print(f"get_or_load with closure: {count_or_load}")
print(f"get_or_load without closure: {count_bad}")
print(f"double brace closings: {count_braces}")
print("OK: metadata_abi.rs re-fixed")
