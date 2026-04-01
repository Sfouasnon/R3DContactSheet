from setuptools import setup


APP = ["app.py"]
OPTIONS = {
    "argv_emulation": False,
    "iconfile": None,
    "plist": {
        "CFBundleName": "R3D Contact Sheet",
        "CFBundleDisplayName": "R3D Contact Sheet",
        "CFBundleIdentifier": "com.sfouasnon.r3dcontactsheet",
        "CFBundleShortVersionString": "0.2.0",
        "CFBundleVersion": "0.2.0",
        "LSMinimumSystemVersion": "12.0",
    },
    "packages": ["r3dcontactsheet"],
}


setup(
    name="R3DContactSheet",
    app=APP,
    options={"py2app": OPTIONS},
)
