# Third-party components

The MIT license in `LICENSE` covers **only the original code** of this repository (`app/`, `tests/`,
`examples/`, `deploy/` and project files).

The repository **does not include** third-party code: dependencies are downloaded at install time
(`pip install`) or when building the Docker image (`apt-get`, `python` base image). Each component remains
subject to its own license.

## Python dependencies (installed from PyPI)

| Package | License |
|---|---|
| FastAPI | MIT |
| Starlette | BSD-3-Clause |
| Uvicorn | BSD-3-Clause |
| Pydantic / pydantic-core / pydantic-settings | MIT |
| python-multipart | Apache-2.0 |
| defusedxml | PSF-2.0 |
| pytest (development only) | MIT |
| httpx / httpx2 (development only) | BSD-3-Clause |

Transitive dependencies (e.g. anyio, h11, click, typing-extensions) use permissive licenses; the exact list
depends on the resolved versions and can be obtained with `pip-licenses` or `pip show <package>`.

## Components bundled in the Docker image

The image built from the `Dockerfile` contains third-party software **distributed together** with the project:

| Component | License |
|---|---|
| Debian GNU/Linux (bookworm) and system packages | various (GPL, LGPL, MIT, BSD, ...) |
| Python 3.12 (official `python` image) | PSF License |
| LibreOffice (`libreoffice-*-nogui`) | MPL-2.0 (some parts LGPLv3+ / other licenses) |
| DejaVu fonts | Bitstream Vera / public domain |
| Liberation 2 fonts | SIL Open Font License 1.1 |
| Carlito, Caladea fonts | SIL Open Font License 1.1 |

### Obligations when distributing the Docker image

Whoever **publishes or distributes** an image built from this project (e.g. on a public registry) also
distributes the components above and must comply with their licenses, in particular:

- **keep the copyright notices and license texts** shipped in the image (for Debian packages in
  `/usr/share/doc/<package>/copyright`);
- for **MPL-2.0 / LGPL / GPL** components (LibreOffice and several Debian packages), make the **corresponding
  source code** available or provide a written offer to obtain it. For unmodified Debian packages it is common
  practice to point to the Debian source packages (`apt-get source <package>`, https://sources.debian.org);
- for fonts under the **OFL 1.1**, do not sell them on their own and keep the license.

Using the service internally, without distributing the image, normally does not trigger source distribution
obligations. This section is informational and is not legal advice.

## Native installation

In native mode LibreOffice is installed separately by the user from the official website
(https://www.libreoffice.org) or the system package manager, and is not redistributed by this project.

"LibreOffice" is a registered trademark of The Document Foundation. This project is not affiliated with or
endorsed by The Document Foundation. "Microsoft Office", "Word", "Excel" and "PowerPoint" are trademarks of
Microsoft Corporation; the DOCX, XLSX and PPTX formats are mentioned only to describe compatibility.
