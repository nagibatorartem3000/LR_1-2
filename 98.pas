Var x1,x2,y1,y2: integer;
begin
  write('Введите число');
read (x1,y1,x2,y2);
 if (x1>0) and (y1>0) and (x2>0) and (y2>0) then 
   writeln ('YES')
  else writeln ('NO');
 if (x1<0) and (y1>0) and (x2<0) and (y2>0) then 
   writeln ('YES')
  else writeln ('NO');
 if (x1>0) and (y1<0) and (x2>0) and (y2<0) then 
   writeln ('YES')
  else writeln ('NO');
 if (x1<0) and (y1<0) and (x2<0) and (y2<0) then 
   writeln ('YES')
  else writeln ('NO');

End.