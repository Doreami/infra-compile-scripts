path = "/home/xl/iceberg-og/iceberg-rust-bridge/src/sdk/storage_scope.rs"
with open(path, "r") as f:
    content = f.read()

# Fix b0 -> &[0u8][..]
content = content.replace("hasher.update(b0);", "hasher.update(&[0u8][..]);")
# Fix all three occurrences
# Actually let me just do a global replace
content = content.replace("b0", "&[0u8][..]")

with open(path, "w") as f:
    f.write(content)

print("OK: storage_scope.rs fixed")
