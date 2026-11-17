"""Mascaramento de dados pessoais para logs e interface (LGPD)."""

import re


def mascarar_cnpj(cnpj: str | None) -> str | None:
    """12345678000195 -> 12.345.***/****-95"""
    if not cnpj:
        return cnpj
    digitos = re.sub(r"\D", "", cnpj)
    if len(digitos) != 14:
        return "***"
    return f"{digitos[:2]}.{digitos[2:5]}.***/****-{digitos[12:]}"


def mascarar_email(email: str | None) -> str | None:
    """joao@empresa.com.br -> jo***@empresa.com.br"""
    if not email:
        return email
    usuario, separador, dominio = email.partition("@")
    if not separador or not usuario or not dominio:
        return "***"
    return f"{usuario[:2]}***@{dominio}"
