# Cargo mirror

A stop-gap tool for making mirrors for Rust.


## WARNING

Using this script is not nice on crates.io server.


## Install

```bash
python setup.py install        # may require admin rights
python setup.py install --user # only for current user
```

## Use

```
cargo-mirror new mymiror
cargo-mirror update mymiror
```

alternatively use `python -m cargo_mirror` instead of `cargo-mirror`
