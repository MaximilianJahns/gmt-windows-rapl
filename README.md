# Windows RAPL → GMT Integration

Dieses Projekt ermöglicht die Messung von CPU-Energieverbrauch auf Windows-Rechnern mit dem [Green Metrics Tool (GMT)](https://github.com/green-coding-solutions/green-metrics-tool).

## Architektur

```
Windows Kernel
  └── ScaphandreDrv.sys       (RAPL MSR Kernel-Treiber)
        └── IOCTL: \\.\ScaphandreDriver
              └── rapl_reader.exe   (C, liest MSR direkt)
                    │ stdout: timestamp_us value detail_name
                    │ via cmd.exe Aufruf
WSL2 Ubuntu
  └── metric-provider-binary  (Bash Wrapper, 5 Zeilen)
        └── GMT BaseMetricProvider
              └── runner.py → PostgreSQL → Frontend
```

**Warum cmd.exe Wrapper?** WSL2-Linux kann nicht direkt auf Windows Kernel Device Files zugreifen (`ctypes.windll` nicht verfügbar). WSL2 kann aber Windows `.exe` direkt aufrufen – kein HTTP Server, keine Firewall-Regel nötig.

## Repos

| Repo | Inhalt |
|------|--------|
| [gmt-windows-rapl](https://github.com/MaximilianJahns/gmt-windows-rapl) | `rapl_reader_cli.c`, `build_and_deploy.bat`, `scaphandre_driver_win64.zip` |
| [green-metrics-tool_rapl_metric_provider](https://github.com/MaximilianJahns/green-metrics-tool_rapl_metric_provider) | `provider.py`, `metric-provider-binary` |

## Messwerte

| Domain | detail_name | Beschreibung |
|--------|-------------|--------------|
| Package | `cpu_package` | Gesamte CPU |
| Cores | `cpu_cores` | Nur Rechenkerne |
| iGPU | `cpu_gpu` | Integrierte Grafik |
| DRAM | `dram` | Arbeitsspeicher |

Einheit: **µJ** (Mikro-Joule), GMT wandelt automatisch in mWh um.

## Schnellstart

### Windows (PowerShell als Admin)

```powershell
# 1. Testmodus aktivieren (einmalig, dann Neustart)
bcdedit.exe -set TESTSIGNING ON
bcdedit.exe -set nointegritychecks on
```

```powershell
# 2. Treiber-Dateien bereitstellen
# Option A: ZIP aus dem Repo entpacken (empfohlen)
#   → scaphandre_driver_win64.zip entpacken nach x64\Debug\
#
# Option B: Selbst kompilieren (Visual Studio 2022 + WDK nötig)
#   → ScaphandreDrv.sln öffnen → Release/x64 → Build
```

```powershell
# 3. rapl_reader.exe kompilieren (x64 Native Tools Command Prompt for VS 2022)
cd rapl_reader\
.\build_and_deploy.bat   # → C:\rapl\rapl_reader.exe
```

```powershell
# 4. Treiber installieren + starten
cd x64\Debug\
.\DriverLoader.exe install
& "$env:SystemRoot\System32\sc.exe" config ScaphandreDrv start= demand
& "$env:SystemRoot\System32\sc.exe" start ScaphandreDrv
```

### WSL2 Ubuntu

```bash
# 1. Provider installieren
mkdir -p ~/gmt-fresh/metric_providers/cpu/energy/rapl/msr/windows
cp provider.py metric-provider-binary __init__.py \
   ~/gmt-fresh/metric_providers/cpu/energy/rapl/msr/windows/
chmod +x ~/gmt-fresh/metric_providers/cpu/energy/rapl/msr/windows/metric-provider-binary
```

```yaml
# 2. config.yml unter common: eintragen
#      cpu.energy.rapl.msr.windows.provider.CpuEnergyRaplMsrWindowsProvider:
#        sampling_rate: 100
#        rapl_reader_exe: 'C:\rapl\rapl_reader.exe'
```

```bash
# 3. GMT starten
cd ~/gmt-fresh && source venv/bin/activate
python3 runner.py --uri ~/gmt-fresh --filename tests/usage_the_test.yml ...
```

## Bekannte Einschränkungen / TODO

| # | Problem | Status | Möglicher Fix |
|---|---------|--------|---------------|
| 1 | Treiber unsigniert → Testmodus nötig | offen | EV-Zertifikat + HLK Prozess |
| 2 | Nur Single-Socket (cpu_index=0) | offen | `-s <socket>` Parameter |
| 3 | `GetTickCount()` hat ~15ms Auflösung | offen | `QueryPerformanceCounter()` |
| 4 | `cpu_gpu` Minimalwert 2µJ wenn Wert 0 | offen | Letzten bekannten Wert wiederholen |
| 5 | Treiber-Start nach Neustart manuell | offen | Windows Autostart-Script |
| 6 | Noch nicht mit Docker Desktop getestet | offen | Test steht aus |
| 7 | stdout-Weiterleitung für viele Metriken ungeeignet | offen | Named Pipe als Alternative prüfen |
| 8 | Sampling Rate Toleranz ±20% – bei Grenzwerten instabil | offen | Letzten gültigen Wert statt Minimalwert 2 verwenden |
| 9 | Latenz durch cmd.exe Wrapper (~5-10ms) | offen | Native WSL2-zu-Windows IPC prüfen |

## Voraussetzungen

- Windows 10/11 (64-bit)
- Visual Studio 2022 mit C++ Workload + WDK 10.0 *(nur für Eigenkompilierung)*
- Anaconda/Miniconda mit Python 3.8+
- WSL2 Ubuntu, Docker, GMT installiert
- Administrator-Zugang (Windows)
