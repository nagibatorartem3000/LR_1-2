var
  i,n,c,b: integer;
begin
  c:=0; b:=0;
  for i:=1 to 3 do
  begin
    write('Введите ',i,' число: ');
    readln(n);
    if n>0 then c:=c+1;
    if n<0 then b:=b+1;
  end;
  writeln('Количество положительных чисел = ',c,', отрицательных = ',b);
end.