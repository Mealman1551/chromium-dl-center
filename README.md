# chromium-dl-center

Overview

chromium-dl-center is a small command line utility to download Chromium builds and related assets from public build archives. It is intended to make it straightforward to fetch a specific revision or the latest build for a given platform.

Features

- Download a specific Chromium revision or the latest available build.
- Choose target platform and architecture.
- Resume interrupted downloads where supported by the source.
- Simple command line interface with help and basic options.

Requirements

- Python 3.8 or newer
- pip
- Internet access to the chosen build archive

Download

You can get the project by cloning the repository or by downloading a ZIP from the repository hosting page.

Clone example:

```
git clone <repository_url>
cd chromium-dl-center
```

Installation

From the project root install the Python dependencies:

```
python -m venv venv
venv\\Scripts\\activate   # on Windows
source venv/bin/activate  # on Linux or macOS
pip install -r requirements.txt
```

Usage

Run the main script and view available options:

```
python chr.py --help
```

Download a specific revision example:

```
python chr.py --revision 123456 --platform win64 --output ./downloads
```

Download the latest build example:

```
python chr.py --latest --platform linux64 --output ./downloads
```

Refer to the command line help for the exact option names supported by this tool.

Support and issues

If you encounter problems, open an issue on the repository issue tracker and include the command you ran and any error output.

License

Check the repository for a license file. If none is present, assume no explicit license.
