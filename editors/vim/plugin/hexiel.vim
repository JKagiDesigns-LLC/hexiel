vim9script
if exists('g:loaded_hexiel') || v:version < 900
  finish
endif
g:loaded_hexiel = 1

command! Hexiel         call hexiel#Open()
command! -nargs=* HexielAsk  call hexiel#Ask(<q-args>)
command! -range -nargs=* HexielSend call hexiel#SendRange(<line1>, <line2>, <q-args>)
command! HexielStop     call hexiel#Stop()
command! HexielNew      call hexiel#NewChat()
command! HexielDiff     call hexiel#Diff()
