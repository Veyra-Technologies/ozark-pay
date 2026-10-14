"""Include the same migration tree in wheels without shadowing Alembic itself."""
from pathlib import Path
from shutil import copy2, copytree

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildWithMigrations(build_py):
    def run(self):
        super().run()
        target = Path(self.build_lib) / 'app' / '_migrations'
        copytree('alembic', target, dirs_exist_ok=True, ignore=lambda _, names: [n for n in names if n == '__pycache__'])
        copy2('alembic.ini', target / 'alembic.ini')
        copytree('branding', Path(self.build_lib) / 'app' / '_branding', dirs_exist_ok=True)


setup(cmdclass={'build_py': BuildWithMigrations})
