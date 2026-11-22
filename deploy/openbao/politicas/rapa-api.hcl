# Interface, REST e MCP do RaPA (rapa-web e rapa-api).
#
# Só desembrulham a chave de uma execução para servir um artefato cifrado a quem vê a
# execução (rapa.control.services.artifacts). Nunca leem segredos de credenciais.

path "rapa-transit/decrypt/empresa-*" {
  capabilities = ["update"]
}

# Renovar o próprio token antes de vencer (o papel não leva a política "default").
path "auth/token/renew-self" {
  capabilities = ["update"]
}
