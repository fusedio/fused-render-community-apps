# Disk Cleaner

A local disk-usage browser and cache cleaner. It shows how full the volume is,
lets you drill down through directories sized on demand, and lists well-known
cache locations (package managers, browsers, build tools) with their sizes so
you can empty the ones you pick — moved to the OS trash by default, or deleted
permanently.

Cleaning only ever targets cache directories from the built-in catalog
(`catalog.py`): it takes catalog ids, never raw paths, needs an explicit
confirmation, and empties a directory's contents while keeping the directory
itself.
