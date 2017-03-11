# -*- coding: utf-8 -*-
from setuptools import setup

setup(name='cargo-mirror',
      version='0.0.1',
      description="Python script to make and maintain a Cargo mirror",
      author="Mickaël Delahaye",
      author_email="mickael.delahaye@gmail.com",
      url="https://github.com/polazarus/cargo-mirror.py",
      py_modules=['cargo_mirror'],
      entry_points={
          'console_scripts': [
              'cargo-mirror = cargo_mirror:main'
          ]
      },
      license="License :: OSI Approved :: MIT License",
      long_description="""TODO""",
)
