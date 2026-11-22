# Operação da plataforma (python -m rapa.control.admin, com a URL do dono das tabelas).
#
# - exportar: desembrulha a chave de cada execução para levar os artefatos decifrados;
# - apagar: apaga os metadados (todas as versões) de todos os segredos da empresa, libera
#   a exclusão da chave transit dela e a apaga (rapa.control.vault.OpenBaoKeyDestroyer).
#
# Nunca lê o valor de um segredo, nunca cria, gira, corta ou exporta uma chave transit.

path "rapa-transit/decrypt/empresa-*" {
  capabilities = ["update"]
}

path "rapa/metadata/empresas/*" {
  capabilities = ["list", "delete"]
}

# A chave em si: conferir se existe e apagar. O "+" é um segmento inteiro: não pega
# <chave>/config, /rotate nem /trim, e sem "update" aqui nenhum POST cria uma chave (um
# glob como keys/empresa-* pegaria tudo isso, e venceria o caminho de /config abaixo).
path "rapa-transit/keys/+" {
  capabilities = ["read", "delete"]
}

# Liberar a exclusão, e só isso: nada de exportable ou allow_plaintext_backup (que não
# têm volta) nem de outro campo da configuração.
path "rapa-transit/keys/+/config" {
  capabilities = ["update"]
  allowed_parameters = {
    "deletion_allowed" = ["true"]
  }
}

# Renovar o próprio token antes de vencer (o papel não leva a política "default").
path "auth/token/renew-self" {
  capabilities = ["update"]
}
