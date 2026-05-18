# BMM - Better Mod Manager

BMM is a lightweight mod manager for Ostranauts. It is built around a simple desktop GUI and a small JSON metadata format so BepInEx plugins and Ostranauts data mods can be installed, enabled, disabled, updated, and removed without editing the game folder by hand.

The current build is intentionally focused on GitHub-hosted mods and local data mod ZIPs.

## Features

- Tracks GitHub release repos and checks for newer mod versions.
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

To add an Ostranauts data mod ZIP:

1. Select the ZIP in the `Data ZIP` field.
2. Press `Add Data Mod`.
3. Install it from the mod list.

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

## Modder Metadata

BMM can auto-detect simple release ZIPs, but proper metadata makes installs safer and more predictable.

For a BepInEx plugin:

- Publish releases on GitHub with a ZIP asset.
- Put the plugin DLL at the ZIP root or under `BepInEx/plugins/`.
- Use a stable lowercase BMM id, such as `author.modname`.
- Include the BepInEx plugin GUID from `[BepInPlugin("guid", "name", "version")]`.
- Declare install targets if the ZIP contains more than one obvious DLL or folder.
- Declare dependencies, conflicts, and provided capability IDs when needed.

For an Ostranauts data mod:

- Put one top-level mod folder in the ZIP.
- Include `mod_info.json` inside that folder.
- BMM installs the folder into `Ostranauts_Data\Mods`.
- BMM enables/disables data mods through `loading_order.json`.

Minimal index shape:

```json
{
  "id": "author.examplemod",
  "name": "Example Mod",
  "summary": "Short description shown in BMM.",
  "authors": ["AuthorName"],
  "categories": ["quality-of-life"],
  "website": "https://github.com/AuthorName/ExampleMod",
  "plugin": {
    "guid": "com.author.ostranauts.examplemod",
    "name": "Example Mod",
    "dll": "ExampleMod.dll"
  },
  "relationships": {
    "depends": [],
    "recommends": [],
    "suggests": [],
    "conflicts": [],
    "provides": ["com.author.ostranauts.examplemod"]
  },
  "release": {
    "provider": "github",
    "repo": "AuthorName/ExampleMod",
    "asset_pattern": "ExampleMod-*.zip",
    "include_prereleases": false
  }
}
```

## Current Limits

- BMM only modifies mods it installed itself.
- External mods are listed read-only unless they are connected to a GitHub repo for metadata.
- BepInEx itself must be installed separately.
- Dependency checks exist, but automatic dependency solving is not implemented yet.
- Deep conflict detection is planned but not complete yet.
