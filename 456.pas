var a, b, c : integer;
begin
  
  writeln('Введите сторону треугольника');
  write('a, b, c ');
  readln(a, b, c);
   if (a+b>c) and (a+c>b) and (b+c>a) then
   begin
     if (aa+bb=cc) and (aa+cc=bb) and (bb+cc=aa) then
     writeln('rectangular');
     else if (aa+bb<cc) and (aa+cc<bb) and (bb+cc<aa) then
     writeln('obtuse')
     else
     writeln('acute');
     end
      else
       writeln('impossible');
end.
   end;
  end.
  