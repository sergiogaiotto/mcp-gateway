{{/*
CIDRs de IPv4 e IPv6 nos templates, para a validação (templates/_validation.tpl) e para as
NetworkPolicies. Um CIDR vira uma lista de blocos de 16 bits (2 em IPv4, 8 em IPv6), e as
contas (bits de host, sobreposição, contido em) são feitas bloco a bloco: o Helm não tem
funções de rede, e um texto que parece CIDR não basta (0.0.0.0/00 é a internet inteira para
o Python e para o Kubernetes).

O texto aceito é o canônico de cada parte, para o chart, o Kubernetes e o Python lerem a
mesma rede: IPv4 com quatro números sem zeros à esquerda; IPv6 com blocos hexadecimais e no
máximo um "::" (sem o IPv4 embutido com pontos); o prefixo sem zeros à esquerda; e os bits
depois do prefixo zerados (10.30.0.0/24, nunca 10.30.0.1/24).
*/}}

{{/*
rapa.cidrParse (text, out): lê o CIDR e escreve em out: ok (true ou false), why (o que está
errado), version (4 ou 6), bits (32 ou 128), prefix e words (os blocos de 16 bits).
*/}}
{{- define "rapa.cidrParse" -}}
{{- $text := .text | toString | trim -}}
{{- $out := .out -}}
{{- $_ := set $out "ok" false -}}
{{- $_ := set $out "text" $text -}}
{{- $octet := "(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])" -}}
{{- $v4 := printf "^%s(\\.%s){3}/(3[0-2]|[12]?[0-9])$" $octet $octet -}}
{{- $v6 := "^[0-9a-fA-F:]+/(12[0-8]|1[01][0-9]|[1-9]?[0-9])$" -}}
{{- if regexMatch $v4 $text -}}
{{- $parts := splitList "/" $text -}}
{{- $o := splitList "." (first $parts) -}}
{{- $words := list (add (mul (atoi (index $o 0)) 256) (atoi (index $o 1))) (add (mul (atoi (index $o 2)) 256) (atoi (index $o 3))) -}}
{{- $_ := set $out "version" 4 -}}
{{- $_ := set $out "bits" 32 -}}
{{- $_ := set $out "prefix" (atoi (last $parts)) -}}
{{- $_ := set $out "words" $words -}}
{{- $_ := set $out "ok" true -}}
{{- else if regexMatch $v6 $text -}}
{{- $parts := splitList "/" $text -}}
{{- $addr := lower (first $parts) -}}
{{- $bad := contains ":::" $addr -}}
{{- $left := list -}}
{{- $right := list -}}
{{- $halves := splitList "::" $addr -}}
{{- if gt (len $halves) 2 -}}
{{- $bad = true -}}
{{- else if eq (len $halves) 2 -}}
{{- if first $halves }}{{ $left = splitList ":" (first $halves) }}{{ end -}}
{{- if last $halves }}{{ $right = splitList ":" (last $halves) }}{{ end -}}
{{- if gt (add (len $left) (len $right)) 7 }}{{ $bad = true }}{{ end -}}
{{- else -}}
{{- $left = splitList ":" $addr -}}
{{- if ne (len $left) 8 }}{{ $bad = true }}{{ end -}}
{{- end -}}
{{- $hex := dict "0" 0 "1" 1 "2" 2 "3" 3 "4" 4 "5" 5 "6" 6 "7" 7 "8" 8 "9" 9 "a" 10 "b" 11 "c" 12 "d" 13 "e" 14 "f" 15 -}}
{{- $words := list -}}
{{- $fill := sub 8 (add (len $left) (len $right)) -}}
{{- $groups := concat $left (list "fill") $right -}}
{{- range $group := $groups -}}
{{- if eq $group "fill" -}}
{{- range until (int $fill) }}{{ $words = append $words 0 }}{{ end -}}
{{- else if regexMatch "^[0-9a-f]{1,4}$" $group -}}
{{- $value := 0 -}}
{{- range $digit := splitList "" $group }}{{ $value = add (mul $value 16) (index $hex $digit) }}{{ end -}}
{{- $words = append $words $value -}}
{{- else -}}
{{- $bad = true -}}
{{- end -}}
{{- end -}}
{{- if not $bad -}}
{{- $_ := set $out "version" 6 -}}
{{- $_ := set $out "bits" 128 -}}
{{- $_ := set $out "prefix" (atoi (last $parts)) -}}
{{- $_ := set $out "words" $words -}}
{{- $_ := set $out "ok" true -}}
{{- end -}}
{{- end -}}
{{- if not $out.ok -}}
{{- $_ := set $out "why" "não é um CIDR (a.b.c.d/n ou x:x::/n, sem zeros à esquerda e sem o IPv4 com pontos dentro do IPv6)" -}}
{{- else -}}
{{- /* Os bits depois do prefixo precisam ser zero. */ -}}
{{- $pow := list 1 2 4 8 16 32 64 128 256 512 1024 2048 4096 8192 16384 32768 65536 -}}
{{- $prefix := int64 $out.prefix -}}
{{- range $i, $word := $out.words -}}
{{- $start := int64 (mul $i 16) -}}
{{- if le $prefix $start -}}
{{- if ne (int64 $word) 0 }}{{ $_ := set $out "ok" false }}{{ end -}}
{{- else if lt $prefix (add $start 16) -}}
{{- $step := index $pow (int (sub 16 (sub $prefix $start))) -}}
{{- if ne (int64 (mod $word $step)) 0 }}{{ $_ := set $out "ok" false }}{{ end -}}
{{- end -}}
{{- end -}}
{{- if not $out.ok -}}
{{- $_ := set $out "why" "tem bits ligados depois do prefixo (use o endereço da rede)" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
rapa.cidrOverlaps (a, b: CIDRs lidos): "true" quando as duas redes têm algum endereço em
comum (uma contém a outra: os primeiros min(prefixos) bits são iguais).
*/}}
{{- define "rapa.cidrOverlaps" -}}
{{- if and .a.ok .b.ok (eq (int .a.version) (int .b.version)) -}}
{{- $pow := list 1 2 4 8 16 32 64 128 256 512 1024 2048 4096 8192 16384 32768 65536 -}}
{{- $k := int64 (min (int64 .a.prefix) (int64 .b.prefix)) -}}
{{- $other := .b.words -}}
{{- $same := true -}}
{{- range $i, $word := .a.words -}}
{{- $start := int64 (mul $i 16) -}}
{{- $peer := index $other $i -}}
{{- if ge $k (add $start 16) -}}
{{- if ne (int64 $word) (int64 $peer) }}{{ $same = false }}{{ end -}}
{{- else if gt $k $start -}}
{{- $step := index $pow (int (sub 16 (sub $k $start))) -}}
{{- if ne (int64 (div $word $step)) (int64 (div $peer $step)) }}{{ $same = false }}{{ end -}}
{{- end -}}
{{- end -}}
{{- if $same }}true{{ end -}}
{{- end -}}
{{- end -}}

{{/* rapa.cidrWithin (a, b: CIDRs lidos): "true" quando a rede a fica toda dentro de b. */}}
{{- define "rapa.cidrWithin" -}}
{{- if and (include "rapa.cidrOverlaps" .) (ge (int64 .a.prefix) (int64 .b.prefix)) -}}
true
{{- end -}}
{{- end -}}

{{/*
rapa.cidrCheck (text, what, minV4, minV6, forbidden, within): confere um CIDR de um valor e
falha com a mensagem do que corrigir. minV4 e minV6 são os menores prefixos aceitos;
forbidden, as redes que ele não pode tocar; within (opcional, só IPv4), as redes em que um
CIDR de IPv4 precisa caber.
*/}}
{{- define "rapa.cidrCheck" -}}
{{- $net := dict -}}
{{- include "rapa.cidrParse" (dict "text" .text "out" $net) -}}
{{- if not $net.ok -}}
{{- fail (printf "%s: %q %s" .what (toString .text) $net.why) -}}
{{- end -}}
{{- $min := ternary .minV4 .minV6 (eq (int $net.version) 4) -}}
{{- if lt (int $net.prefix) (int $min) -}}
{{- fail (printf "%s: %q é largo demais (o prefixo precisa ser /%d ou mais estreito)" .what $net.text (int $min)) -}}
{{- end -}}
{{- range $bad := .forbidden -}}
{{- $b := dict -}}
{{- include "rapa.cidrParse" (dict "text" $bad "out" $b) -}}
{{- if include "rapa.cidrOverlaps" (dict "a" $net "b" $b) -}}
{{- fail (printf "%s: %q toca %s, uma rede que nunca se abre (loopback, link-local e metadata da nuvem, NAT64, 6to4, multicast)" $.what $net.text $bad) -}}
{{- end -}}
{{- end -}}
{{- if and .within (eq (int $net.version) 4) -}}
{{- $inside := false -}}
{{- range $allowed := .within -}}
{{- $w := dict -}}
{{- include "rapa.cidrParse" (dict "text" $allowed "out" $w) -}}
{{- if include "rapa.cidrWithin" (dict "a" $net "b" $w) }}{{ $inside = true }}{{ end -}}
{{- end -}}
{{- if not $inside -}}
{{- fail (printf "%s: %q precisa ficar dentro de %s" .what $net.text (join ", " .within)) -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
rapa.nat64 (text): a rede de IPv4 dentro do prefixo NAT64 64:ff9b::/96 (o mesmo destino,
alcançado por IPv6 num cluster com NAT64 e DNS64). Ex.: 203.0.113.0/24 vira
64:ff9b::cb00:7100/120.
*/}}
{{- define "rapa.nat64" -}}
{{- $net := dict -}}
{{- include "rapa.cidrParse" (dict "text" .text "out" $net) -}}
{{- printf "64:ff9b::%x:%x/%d" (int64 (index $net.words 0)) (int64 (index $net.words 1)) (add 96 (int64 $net.prefix)) -}}
{{- end -}}
