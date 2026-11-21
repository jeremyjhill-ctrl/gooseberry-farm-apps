"""Which build (git commit) is running, for the small "Build 1a2b3c4" line at the bottom of each page.

- On Render: Render tells the app the commit it deployed (RENDER_GIT_COMMIT), so it always matches what's live.
- On the Mac: Publish Farm Apps writes the published commit into a BUILD file in the Farm Apps folder.
- Anywhere else: "local".
"""
import os

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def build():
    commit = os.environ.get("RENDER_GIT_COMMIT", "").strip()
    if commit:
        return commit[:7]
    try:
        with open(os.path.join(_HERE, "BUILD")) as f:
            got = f.read().strip()
            return got[:12] or "local"
    except OSError:
        return "local"


FOOTER = ('<div class="buildtag noprint" style="text-align:center;color:#8A938B;font-size:12px;padding:20px 0 28px;'
          'letter-spacing:.02em">Build {}</div>')


def footer():
    from markupsafe import Markup, escape
    return Markup(FOOTER.format(escape(build())))
