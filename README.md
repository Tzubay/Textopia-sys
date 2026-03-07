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

#### En caso de estar usando el cliente de chat:
Si estas usando el cliente de chat, deberas de abrir una terminal en la direccion del proyecto, una vez en la direccion,
deberas ejecutar el cliente. 

```
python3 chat_client.py --host 127.0.0.1 --port 5050 --nick alice
```
Esto se creara automaticamente tu session con el usuario «alice»
Puedes cambiar el NICK por el que prefieras, nunca uses el mismo NICK 2 veces en diferentes terminales en simultaneo


#### En caso de __NO__ estar usando el cliente de chat:
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

### Ver conexiones activas en el servidor
Para ver todos los usuarios en Linea, tus grupos PRIVADOS o grupos PUBLICOS disponibles, existe un comando para ver todo esto

```
/who
```
Con este comando te muestra todos los usuarios activos, los grupos __PUBLICOS__ activos, y los grupos __PRIVADOS__ de los que eres parte

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

## GRUPOS
Nuestra aplicación cuenta con opciones de crear grupos apartes, ya que en el chat global cualquiera puede leer mensajes
Con la creacion se grupos podemos crear grupos tanto PRIVADOS como PUBLICOS

En los grupos privados necesitamos ser invitados por los miembros, mientras que los publicos podemos entrar en cualquier momento

### Crear Grupos
Para crear grupos, es muy sencillo, solo necesitas el comando 
```
/room [NICK_1,NICK_2,NICK_3,NICK_4,...] name «Nombre_Grupo» status PUBLIC/PRIVATE
```
Por ejemplo:
```
/room [Juan,Jaimico,Emma,Juliana] name Las_Delireñas2 status PRIVATE
```
Donde: 
__/room__: Es el comando que "invoca" la creación de una sala
__[]__: Se colocan los usuarios (En linea) para invitarlos al grupo antes de crearlo. 
__name__: Se define el nombre del grupo
__status__: Aquí se define si el nombre sera ***PUBLIC*** (Cualquiera puede verlo y unirse) o ***PRIVATE*** (Solo los miembros invitados pueden interactuar y ver el grupo)

## Sobre los grupos
En los grupos hay varias opciones por probar. 
Por ejemplo
### Entrar a un grupo publico
Para entrar a un grupo ***publico*** podemos usar el comando __/intro__

Por ejemplo:
```
/intro Las_Delireñas2
```
(Solo funciona con grupos PUBLICOS)

### Invitar a una persona en Linea a un grupo __DESPUES__ de haberlo creado (PUBLIC o PRIVATE)
Despues de haber creado el grupo e invitar a los usuarios originales, puedes invitar a nuevas personas a unirse (Solo las personas unidas al grupo pueden hacer esto)
```
/invite [NICK] room «Nombre_Grupo»
```

Por ejemplo:
```
/invite [diego] room Las_Delireñas2
```

Donde: 
__/invite__: Es el comando que "invoca" la acción de añadir a un usuario
__[]__: Se colocan los usuarios (En linea) para invitarlos al grupo despues de crearlo. 
__room__: Se especifica el nombre del grupo a donde lo quieres invitar

### Salir permanentemente del grupo

Si quieres dejar de estar en el grupo, salir para siempre de este grupo (a menos que te vuelvan a invitar) existe un comando para esto

```
/quitroom «Nombre_Grupo»
```

Por ejemplo

```
/quitroom Las_Delireñas2
```

## Para mas informacion 
Estos son los comandos mas basicos, pero hay mas, puedes usar el comando ***/help*** para obtener mas información sobre los comandos
```
/help
```