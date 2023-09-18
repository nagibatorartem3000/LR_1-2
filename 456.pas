var a, b, c : integer;
begin
  
  writeln('Введите сторону треугольника');
  writeln('a, b, c ');
  readln(a, b, c);
   if (a+b>c) and (a+c>b) and (b+c>a) then
   begin
   if (a*a+b*b=c*c) and (a*a+c*c=b*b) and (b*b+c*c=a*a) then
   writeln('rectangular(прямоугольный)')
   else if (a*a+b*b<c*c) and (a*a+c*c<b*b) and (b*b+c*c<a*a) then
   writeln('obtuse(тупоугольный)')
   else
     writeln('acute(остроугольный)');
     end
     else
     writeln('impossible(нет треугольника)');
   
  end.
  