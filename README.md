# OPAL — data collection software

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Release](https://img.shields.io/github/v/release/iislab0115/OPAL?include_prereleases&label=Release)](https://github.com/iislab0115/OPAL/releases)
[![Last commit](https://img.shields.io/github/last-commit/iislab0115/OPAL)](https://github.com/iislab0115/OPAL/commits/main)
[![Python](https://img.shields.io/badge/Python-3.9%2B-blue.svg)](https://www.python.org/)
[![Cite](https://img.shields.io/badge/Cite-CITATION.cff-informational.svg)](CITATION.cff)
[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.23032923.svg)](https://doi.org/10.5281/zenodo.23032923)

Software used to collect and prepare **OPAL**, a multimodal ambient sensor dataset for activity,
occupancy and energy analysis in an office workspace, described in the accompanying Data Descriptor.

This repository consolidates the three tools that were previously maintained separately. Each tool
keeps its own README, requirements and history; this page explains how they fit together.

| Directory | What it does | Produces |
|---|---|---|
| [`data-collection/`](data-collection) | Polls Shelly smart plugs over the local network at 1 s and SmartThings devices (plugs, motion sensors, door-contact sensors, the stick vacuum, camera event channels) through the SmartThings API at 60 s. Includes a live dashboard and a supervisor that restarts a stalled collector. | `SMP*`, `MS*`, `DS*`, `Camera_*`, `Stick vacuum_*` daily CSV files |
| [`har-datalogger/`](har-datalogger) | Application the three primary participants used to record activity transitions in real time, with automated refinement of the raw self-report log. | the `START`/`STOP` candidate intervals behind `activity_*.csv` |
| [`homecam-media-converter/`](homecam-media-converter) | Converts CN Core HomeCam `.media` files to MP4 (H.264 + AAC). | the reference video used for annotation, which is **not** part of the public dataset |

## Relationship to the dataset

The released dataset is deposited separately, because it is 1.96 GB and is versioned and cited on its
own. This repository is the software; the dataset is the data.

- Dataset: <https://doi.org/10.5281/zenodo.23035057> — released under CC BY 4.0, separately from this MIT-licensed code
- Data Descriptor: under review; this line carries its DOI once it is published

Nothing in this repository is required to *use* the dataset. The released CSV files are
self-describing and documented by the data dictionary that ships with them. This code is here so that
the acquisition can be audited and reproduced.

## What is deliberately not here

No credentials, tokens, OAuth files, collected measurements or participant identifiers are committed.
`data-collection/config.json` is a placeholder template; real values belong in `config.local.json`,
which is git-ignored. Raw video and audio are likewise absent — the converter is included, the media
are not.

## Licence

MIT, for all three tools. See [`LICENSE`](LICENSE) and the per-directory licence files where present.
The **dataset** is released separately under CC BY 4.0; the two licences are not interchangeable.

## Upstream history

Each directory was imported with `git subtree`, so its upstream commits are preserved in this
repository's history. The original repositories are:

- `data-collection/` ← <https://github.com/lime9903/data-collection>
- `har-datalogger/` ← <https://github.com/jyoung531/HAR-DataLogger>
- `homecam-media-converter/` ← <https://github.com/haewonlii/homecam-media-converter>

Those repositories are superseded by this one. To pull a later upstream change into a directory:

```bash
git subtree pull --prefix=data-collection https://github.com/lime9903/data-collection.git main --squash
```

## Citation

If you use this software, cite the archived version rather than the repository head, so that the
code you cite is the code you ran:

> Kim, J., Lee, H., Lee, J., Roh, J. & Hwang, E. *OPAL Data Collection Software* (v1.0.0).
> Zenodo. <https://doi.org/10.5281/zenodo.23032924> (2026).

Machine-readable metadata is in [`CITATION.cff`](CITATION.cff). The badge above points at the
concept DOI, `10.5281/zenodo.23032923`, which always resolves to the newest archived version.

The **dataset** is deposited separately under its own DOI, <https://doi.org/10.5281/zenodo.23035057>. Cite that when you use
the data, and this software only when you reuse or audit the acquisition code.
