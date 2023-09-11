var a, b, c : integer;
begin
  writeln('Введите 3 стороны a, b, c');
  read(a, b, c);
  if (a+b>c) and (a+c>b) and (b+c>a) then
    writeln('Треугольник с такими сторонами существует')
  else
    writeln('Треугольника с такими сторонами не существует');
end.