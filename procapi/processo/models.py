# -*- coding: utf-8 -*-
from __future__ import absolute_import, print_function, unicode_literals

from mongoengine import (
    Document,
    StringField
)
from config.settings.common import mongo_conn


class Processo(Document):
    numero = StringField(required=True)
    chave = StringField(max_length=50)
