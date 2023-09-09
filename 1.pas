Var s,x: integer;
begin
write ('Введите четырхзначное число = ');
read (x);
s:=(x div 1000) + ((x div 100) mod 10) + ((x div 10) mod 10) + (x mod 10);
writeln('Сумма = ',s);
readln
end.