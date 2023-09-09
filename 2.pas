var i: integer;
begin
write ( 'Введите номер текущего года: ' );
readln (i);
if i mod 4 = 0 then writeln ('Данный год является високосным.')
else writeln ('Данный год не является високосным.')
end.