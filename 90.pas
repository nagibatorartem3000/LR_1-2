var a, b, c, d, e, f: integer;
begin
writeln('Введите стоимость товара (руб. коп.): ');
readln(a, b);
writeln('Введите сумму оплаты (руб. коп.): ');
readln(c, d);
e := c - a;
f := d - b;
if f < 0 then
begin
e := e - 1; 
f := f + 100; 
end;
writeln('Сдача: ', e, ' руб. ', f, ' коп.');
readln;
end.