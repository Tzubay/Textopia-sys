# ¿Comó usar?

### Obtener el repo
```
git clone git@github.com:Tzubay/Textopia-sys.git
```

### Acceder a la carpeta
```
cd Textopia-sys
```

Una vez dentro de la carpeta del repo, debemos primero y antes que todo encender el servidor

### Encender el servidor 

```
python3 chat_server.py --port 5050
```

### Como mandar menssajes al servidor?

Antes de comenzar a chatear, depemos acceder al Socket (en una terminal diferente a la que iniciaste el servidor)

```
nc 127.0.0.1 5050
```

Despues necesitamos crear un Nick Name

```
NICK «Tu_NICK_Name»
```

por ejemplo:

```
NICK User_1
```
Con esto, el usuario se une al Chat Global
Para tener conversacion entre 2 usuarios, inicia otra terminal, y repite los mismos pasos para Acceder sl Socket, crear un 
Usuario diferente, y de nuevo accede al chat global. 
Ahora, todos los usuarios pueden chatear al mismo chat grupal

```
nc 127.0.0.1 5050
```

Despues necesitamos crear un Nick Name

```
NICK User_2
```

Con esto, ambos usuarios pueden comunicarse, pero cualquier usuario nuevo que acceda, puede ver y escribir mensajes 

### Mensajes privados 
Los usuarios pueden mandarse mensajes privados entre ellos.
Para eso, usamos el @ Seguido del NICK del usuario a enviar mensaje

```
@«NICK» Hola, amigo
```

Por ejemplo:

```
@User_1 Hola, Usuario 1
```

En la terminal del usuario 2 deberias ver el mensaje que llega desde el usuario User_1

```
@User_2 Hola, Usuario 2
```