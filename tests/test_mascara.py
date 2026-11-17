from recebiveis.mascara import mascarar_cnpj, mascarar_email


def test_mascara_cnpj_no_formato_do_prompt():
    assert mascarar_cnpj("12345678000195") == "12.345.***/****-95"
    assert mascarar_cnpj("12.345.678/0001-95") == "12.345.***/****-95"


def test_mascara_cnpj_invalido_ou_vazio():
    assert mascarar_cnpj("123") == "***"
    assert mascarar_cnpj(None) is None
    assert mascarar_cnpj("") == ""


def test_mascara_email_no_formato_do_prompt():
    assert mascarar_email("joao@empresa.com.br") == "jo***@empresa.com.br"
    assert mascarar_email("a@x.com") == "a***@x.com"


def test_mascara_email_invalido_ou_vazio():
    assert mascarar_email("sem-arroba") == "***"
    assert mascarar_email(None) is None
