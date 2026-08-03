from time import perf_counter

# Imported by the CLI before service-specific modules so startup metrics include
# application import and construction time.
PROCESS_STARTED_AT = perf_counter()
