# BMM - Better Mod Manager

BMM is a lightweight mod manager for Ostranauts. It is built around a simple desktop GUI and a small JSON metadata format so BepInEx plugins and Ostranauts data mods can be installed, enabled, disabled, updated, and removed without editing the game folder by hand.

The current build is intentionally focused on GitHub-hosted mods and local data mod ZIPs

## Features

- Tracks GitHub release repos and checks for newer mod versions.
- Reads `bmm.nest.json` metadata so one repo can expose one or more BMM mods.
- Installs and uninstalls BepInEx plugin ZIPs into safe BepInEx folders.
- Enables and disables BMM-managed plugins and Ostranauts data mods.
- Lists existing external BepInEx/data mods without taking ownership of them.
- Supports simple enabled/disabled profiles for installed BMM-managed mods.
- Stores BMM settings, cache, generated index files, and backups in a local `Mod_index` folder.

## Install

Download the latest `BMM.exe` from the GitHub release page when a release is available.

Place it in its own folder, for example:

```text
BMM\
  BMM.exe
  Mod_index\
```

Run `BMM.exe`, select the Ostranauts game folder, then check the warning line under the game path. BMM expects BepInEx to already be installed in the game folder.

To add a GitHub mod manually:

1. Paste a GitHub repo URL into the `GitHub` field.
2. Press `Add Repo`.
3. Press `Update GitHub`.
4. Select the mod and use `Install`, `Enable`, `Disable`, `Update`, `Uninstall`, or `Remove`.

The main list is split into two sections:

- `BepInEx / Plugin Mods` shows DLL/plugin mods. These are loaded by BepInEx from `BepInEx\plugins`.
- `Data Mods / Load Order` shows Ostranauts data mods and their actual load-order position.

To add an Ostranauts data mod ZIP:

1. Select the ZIP in the `Data ZIP` field.
2. Press `Add Data Mod`.
3. Install it from the mod list.

Data mods are activated through the game's load-order file at:

```text
Ostranauts_Data\loading_order.json
```

BMM uses that file as the source of truth. If an older `Ostranauts_Data\Mods\loading_order.json` exists, BMM warns about it and migrates its entries into the game-facing file the next time a data mod is installed, enabled, or disabled.

Use `Move Up`, `Move Down`, and `Save Load Order` in the data section to change the order written to that JSON file. `core` is the base game data entry and stays locked at the top.

## Run From Source

Requirements:

- Windows
- Python 3.11 or newer
- No runtime Python packages are required for normal source use

Start the GUI:

```powershell
python .\bmm_gui.py
```

Run the command-line helper:

```powershell
python .\bmm.py list
python .\bmm.py check
python .\bmm.py install <mod-id>
```

## Build

Install PyInstaller in your normal Python environment, then build the one-file GUI executable:

```powershell
python -m PyInstaller --onefile --windowed --name BMM --distpath dist --workpath build --specpath build\spec bmm_gui.py
```

The output will be:

```text
dist\BMM.exe
```

Do not commit `build`, `dist`, `Mod_index`, backups, or generated test folders.

## BMM Nest Files

BMM can still auto-detect very simple ZIPs, but modders should add a nest file for anything public.

Use this file name:

```text
bmm.nest.json
```

Required placement for nest-based GitHub mods:

```text
GitHub repo root/
  bmm.nest.json

Release ZIP/
  bmm.nest.json
  ExampleMod.dll
  Example Data Mod/
    mod_info.json
```

The repo copy lets BMM list and update the mod. The ZIP copy lets BMM verify that the downloaded package is for the selected mod before installing.

A repo can contain one mod or several mods. Each entry in `mods` becomes one row in BMM.

Supported mod types:

- `bepinex` - BepInEx plugin only.
- `data` - Ostranauts data/json mod only.
- `hybrid` - BepInEx plugin plus an Ostranauts data mod folder.

For auto-updates, each mod needs its own `asset_pattern`. This lets one GitHub repo publish multiple ZIPs, such as a full version and a lite version.

### BepInEx Example

```json
{
  "schema": "bmm-nest-v1",
  "mods": [
    {
      "id": "author.examplemod",
      "name": "Example Mod",
      "type": "bepinex",
      "version": "1.0.0",
      "game_versions": ["0.15.x"],
      "summary": "Short description shown in BMM.",
      "authors": ["AuthorName"],
      "categories": ["quality-of-life"],
      "website": "https://github.com/AuthorName/ExampleMod",
      "asset_pattern": "ExampleMod-*.zip",
      "bepinex": {
        "plugin_guid": "com.author.ostranauts.examplemod",
        "name": "Example Mod",
        "dll": "ExampleMod.dll"
      },
      "relationships": {
        "depends": [],
        "conflicts": [],
        "recommends": [],
        "suggests": [],
        "provides": []
      }
    }
  ]
}
```

Expected ZIP:

```text
ExampleMod-1.0.0.zip
  bmm.nest.json
  ExampleMod.dll
```

### Data Mod Example

For a data mod, "top-level mod folder" means the first folder inside the ZIP. It should contain `mod_info.json` directly inside it.

```json
{
  "schema": "bmm-nest-v1",
  "mods": [
    {
      "id": "author.exampledata",
      "name": "Example Data Mod",
      "type": "data",
      "version": "1.0.0",
      "game_versions": ["0.15.x"],
      "summary": "Example Ostranauts JSON/data override.",
      "authors": ["AuthorName"],
      "categories": ["data"],
      "asset_pattern": "ExampleData-*.zip",
      "data": {
        "folder": "Example Data Mod"
      }
    }
  ]
}
```

Expected ZIP:

```text
ExampleData-1.0.0.zip
  bmm.nest.json
  Example Data Mod/
    mod_info.json
    other-data-files.json
```

### Hybrid Example

Use `hybrid` when the same mod needs a BepInEx DLL and an Ostranauts data folder.

```json
{
  "schema": "bmm-nest-v1",
  "mods": [
    {
      "id": "author.examplehybrid",
      "name": "Example Hybrid Mod",
      "type": "hybrid",
      "version": "1.0.0",
      "game_versions": ["0.15.x"],
      "summary": "BepInEx plugin plus data files.",
      "authors": ["AuthorName"],
      "asset_pattern": "ExampleHybrid-*.zip",
      "bepinex": {
        "plugin_guid": "com.author.ostranauts.examplehybrid",
        "dll": "ExampleHybrid.dll"
      },
      "data": {
        "folder": "Example Hybrid Data"
      }
    }
  ]
}
```

Expected ZIP:

```text
ExampleHybrid-1.0.0.zip
  bmm.nest.json
  ExampleHybrid.dll
  Example Hybrid Data/
    mod_info.json
```

### Multiple Mods In One Repo

Use separate entries and separate release ZIP patterns.

```json
{
  "schema": "bmm-nest-v1",
  "mods": [
    {
      "id": "author.fullmod",
      "name": "Full Mod",
      "type": "data",
      "version": "1.0.0",
      "game_versions": ["0.15.x"],
      "asset_pattern": "FullMod-*.zip",
      "data": {
        "folder": "Full Mod"
      }
    },
    {
      "id": "author.litemod",
      "name": "Lite Mod",
      "type": "data",
      "version": "1.0.0",
      "game_versions": ["0.15.x"],
      "asset_pattern": "LiteMod-*.zip",
      "data": {
        "folder": "Lite Mod"
      }
    }
  ]
}
```

Expected release assets:

```text
FullMod-1.0.0.zip
LiteMod-1.0.0.zip
```

Do not put two separate data mods into one ZIP for BMM auto-detect. Use one ZIP per BMM mod entry.

## Current Limits

- BMM only modifies mods it installed itself.
- External mods are listed read-only unless they are connected to a GitHub repo for metadata.
- BepInEx itself must be installed separately.
- Dependency checks exist, but automatic dependency solving is not implemented yet.
- Removing one mod from a multi-mod GitHub repo currently removes the tracked repo source, so all generated rows from that repo disappear.
- Deep conflict detection is planned but not complete yet.
