# Despachante do RaPA (e o lançador que roda nele).
#
# - Lê os segredos das credenciais registradas: sempre debaixo de empresas/<empresa>/,
#   prefixo que o RaPA põe a partir da empresa da execução (rapa.control.vault).
# - Embrulha e desembrulha a chave de cada execução com a chave transit da empresa
#   (empresa-<uuid>; rapa.control.keys). Na primeira vez de uma empresa, o próprio
#   encrypt cria a chave: o transit confere se a chave existe e, sem ela, o pedido é uma
#   criação ("create"), com os padrões do transit (aes256-gcm96, nunca exportável, sem
#   cópia em claro); depois, é uma mudança ("update"). Não ligue disable_upsert em
#   rapa-transit/config/keys.
#
# Nada em rapa-transit/keys/: aquele caminho não confere se a chave existe, então todo
# POST nele é uma mudança, e "update" ali (o glob pega também /config, /rotate e /trim)
# deixaria tornar a chave exportável, girar e cortar versões. Nada de listar, mudar ou
# apagar segredos: isso é da operação (rapa-operacao.hcl).

path "rapa/data/empresas/*" {
  capabilities = ["read"]
}

path "rapa-transit/encrypt/empresa-*" {
  capabilities = ["create", "update"]
}

path "rapa-transit/decrypt/empresa-*" {
  capabilities = ["update"]
}

# Renovar o próprio token antes de vencer (o papel não leva a política "default").
path "auth/token/renew-self" {
  capabilities = ["update"]
}
