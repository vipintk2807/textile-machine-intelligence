# Before connecting any real machine

This release contains **only a simulator adapter**. Do not point it at production equipment. Before writing a real adapter:

1. **Written authorisation** from the machine/plant owner; confirm monitoring is read-only.
2. **Machine documentation** for each model: the signals actually exposed, their meaning, units, scaling, sampling rates.
3. **Interface choice** (e.g. OPC UA, Modbus, MQTT, vendor gateway) confirmed against the machine/controller manual, with
   connection parameters kept outside the repository (no credentials in source control).
4. **Network isolation**: monitoring network separated from control networks; no inbound path to controllers.
5. **State semantics**: how "running" and "stopped" are defined for that machine (a speed signal? a controller flag?), the
   timestamp source and time zone, and whether the source sends heartbeats or only transitions.
6. **Adapter contract** (`src/data_sources/base.py`): read-only; return `RawRecord`s with timezone-aware event times, stable
   `source_event_id`s, explicit units; report failures as `SourceError`, never as an empty list.
7. **Validation on real data** with captured samples, an agreed threshold/limit review with maintenance staff, and a
   comparison of reported runtime against an independent record (e.g. shift logs) before any decision relies on it.
8. **Safety review**: this software is informational; it must not be used for safety functions or to command equipment.
